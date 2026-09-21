"""URL handling for the crawler.

Three jobs, all of which are about not wasting a customer's crawl budget or a
host's patience: decide what is in scope, normalise so the same page is not
fetched twice under different spellings, and refuse the traps that turn a
500-page crawl into an infinite one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

#: Parameters that change nothing about the page. Keeping them would fetch the
#: same content once per campaign link pointing at it.
TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "gclid", "gbraid", "wbraid", "fbclid", "msclkid", "mc_cid",
        "mc_eid", "ref", "_ga", "_gl", "yclid", "igshid", "si",
    }
)

#: Extensions we never want the bytes of. Fetching a 200 MB video to discover
#: it is not HTML is the expensive way to find out.
NON_HTML_SUFFIXES = frozenset(
    {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
        ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z", ".dmg", ".exe", ".apk",
        ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".webm", ".ogg", ".wav", ".m4a",
        ".css", ".js", ".mjs", ".map", ".json", ".xml", ".rss", ".atom",
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv",
        ".woff", ".woff2", ".ttf", ".eot",
    }
)

#: Path segments that mean "this is a calendar/filter/search that generates
#: URLs forever". Not a blocklist of content — a guard against infinite space.
TRAP_SEGMENTS = frozenset({"calendar", "cart", "checkout", "wishlist"})

MAX_PATH_SEGMENTS = 12
MAX_URL_LENGTH = 2048
MAX_QUERY_PARAMS = 6


def url_hash(url: str) -> bytes:
    return hashlib.sha256(url.encode("utf-8")).digest()


def normalise(url: str, *, base: str | None = None) -> str | None:
    """Canonical form of a URL, or None if it is not worth fetching.

    Normalising is what stops `/about`, `/about/`, `/about?utm_source=x` and
    `/ABOUT` being four fetches of one page.
    """
    if not url or url.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None

    if base:
        url = urljoin(base, url)

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.hostname:
        return None

    host = parts.hostname.lower().rstrip(".")
    # A default port is noise that would split the same page into two rows.
    port = parts.port
    if port and not ((parts.scheme == "http" and port == 80) or
                     (parts.scheme == "https" and port == 443)):
        host = f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    # Trailing slash is kept: on many sites `/a` and `/a/` genuinely differ,
    # and the canonical tag is what resolves it, not a guess here.

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    query.sort()

    # The fragment is dropped: it never reaches the server.
    cleaned = urlunsplit((parts.scheme, host, path, urlencode(query), ""))
    return cleaned if len(cleaned) <= MAX_URL_LENGTH else None


@dataclass(frozen=True, slots=True)
class ScopeRule:
    """What counts as "this website".

    Subdomains are out of scope by default. A blog on `blog.example.com` is
    usually a different site with its own Search Console property, and
    silently crawling it spends the customer's page budget on pages they did
    not ask about.
    """

    host: str
    include_subdomains: bool = False

    def covers(self, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host:
            return False
        bare = self.host[4:] if self.host.startswith("www.") else self.host
        if host in (self.host, bare, f"www.{bare}"):
            return True
        return self.include_subdomains and host.endswith("." + bare)


def looks_like_a_trap(url: str) -> str | None:
    """Why this URL would waste the crawl, or None if it is fine."""
    parts = urlsplit(url)
    path = parts.path.lower()

    if any(path.endswith(suffix) for suffix in NON_HTML_SUFFIXES):
        return "non_html"

    segments = [s for s in path.split("/") if s]
    if len(segments) > MAX_PATH_SEGMENTS:
        return "path_too_deep"

    # The same segment repeating is the signature of a relative-link loop:
    # /shop/shop/shop/product.
    for i in range(len(segments) - 2):
        if segments[i] == segments[i + 1] == segments[i + 2]:
            return "repeating_segments"

    if any(s in TRAP_SEGMENTS for s in segments):
        return "infinite_space"

    params = parse_qsl(parts.query, keep_blank_values=True)
    if len(params) > MAX_QUERY_PARAMS:
        # Faceted navigation multiplies: colour x size x brand x page is a
        # combinatorial explosion, not a set of pages worth auditing.
        return "too_many_parameters"

    return None
