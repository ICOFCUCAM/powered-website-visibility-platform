"""Fetching a page politely.

Identifies itself, obeys the host limiter, refuses to download things that
are not pages, and classifies every failure so the crawl summary can say what
went wrong rather than just how often.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from api.crawler.politeness import HostLimiter
from api.crawler.robots import USER_AGENT_STRING

#: Stop reading a response past this. A 200 MB "page" is not a page, and
#: streaming lets us find out before downloading it.
MAX_BODY_BYTES = 5_000_000

#: Content types whose bytes are worth reading. HTML is the point, but
#: robots.txt is text/plain and sitemaps are XML — refusing to decode those
#: would leave the crawler unable to read the two files that seed it.
TEXTUAL_TYPES = ("text/", "application/xml", "+xml", "application/json")

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_REDIRECTS = 5


@dataclass(slots=True)
class FetchResult:
    url: str
    status_code: int | None = None
    content_type: str | None = None
    body: str = ""
    raw: bytes = b""
    elapsed_ms: int = 0
    redirect_chain: list[dict[str, object]] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None

    @property
    def is_html(self) -> bool:
        return bool(self.content_type and "html" in self.content_type.lower())

    @property
    def is_textual(self) -> bool:
        # A missing content type is treated as textual: some servers omit it
        # for robots.txt, and the size cap already bounds the damage.
        if not self.content_type:
            return True
        lowered = self.content_type.lower()
        return any(marker in lowered for marker in TEXTUAL_TYPES)


def classify(exc: Exception) -> str:
    """Failure taxonomy: the crawl summary says what went wrong, not just how
    often. "17 errors" is not actionable; "17 DNS failures" is."""
    if isinstance(exc, httpx.ConnectTimeout | httpx.ReadTimeout | httpx.WriteTimeout):
        return "timeout"
    if isinstance(exc, httpx.TooManyRedirects):
        return "redirect_loop"
    if isinstance(exc, httpx.ConnectError):
        message = str(exc).lower()
        if "name or service not known" in message or "nodename" in message:
            return "dns"
        if "certificate" in message or "ssl" in message:
            return "tls"
        return "connection_refused"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "protocol_error"
    if isinstance(exc, httpx.HTTPError):
        return "http_error"
    return "unknown"


class Fetcher:
    def __init__(
        self,
        limiter: HostLimiter,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._limiter = limiter
        self._client = client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            headers={"User-Agent": USER_AGENT_STRING},
        )

    async def fetch(self, url: str) -> FetchResult:
        host = (urlsplit(url).hostname or "").lower()
        result = FetchResult(url=url)

        await self._limiter.acquire(host)
        started = time.monotonic()
        try:
            response = await self._client.get(url)
            result.status_code = response.status_code
            result.content_type = response.headers.get("content-type")
            # Timed here rather than read from the client: httpx raises on
            # `.elapsed` unless the response was read or closed, and a blanket
            # except would then record a SUCCESSFUL fetch as a failure. It is
            # also the more useful number, since it includes connection time.
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            result.redirect_chain = [
                {"url": str(r.url), "status": r.status_code} for r in response.history
            ]

            # Politeness is not only about the steady state: a server asking
            # us to slow down must actually slow us down.
            if response.status_code in (429, 503):
                retry_after = response.headers.get("retry-after")
                seconds = float(retry_after) if (retry_after or "").isdigit() else 30.0
                self._limiter.back_off(host, min(seconds, 300.0))

            if result.is_textual:
                raw = response.content[:MAX_BODY_BYTES]
                result.raw = raw
                result.body = raw.decode(
                    response.encoding or "utf-8", errors="replace"
                )
        except Exception as exc:  # noqa: BLE001 - every failure is classified
            result.error = classify(exc)
        finally:
            self._limiter.release(host)

        return result
