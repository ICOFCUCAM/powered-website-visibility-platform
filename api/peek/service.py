"""One page, fetched and checked, for somebody who has not signed up.

The point is that the front page stops lying. A marketing dashboard full of
invented numbers is exactly what this product says it will not do, so the
hero runs a real check on a real site and shows what it actually found.

THREE THINGS MAKE THIS DIFFERENT from the crawler proper, and all three are
consequences of there being no account behind the request:

  It fetches ONE page. No frontier, no sitemap, no links followed. A stranger
  cannot spend our bandwidth on a thousand pages of somebody else's site.

  It goes through `safety.check_url` and `safety.check_peer`. The crawler is
  gated on proven ownership; this is gated on nothing, so the URL guard is
  the only thing between us and our own metadata endpoint.

  It writes nothing. No organisation, no website row, no pages, no findings.
  The result exists for the length of the response and is then gone.

What it does NOT do is re-implement any rule. The findings come from the same
`api.analysis.rules` the product runs, over an `AnalysisContext` built from
the one page — so a peek can never disagree with what the real audit would
say about the same page.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from api.analysis.rules import (  # noqa: F401 — registers the rules
    ai_visibility,
    content,
    search,
    technical,
)
from api.analysis.rules.base import AnalysisContext, Finding, PageRow, all_rules
from api.crawler import extract
from api.crawler.robots import USER_AGENT_STRING
from api.crawler.safety import check_peer, check_url

#: A homepage that has not answered in this long is not going to.
TIMEOUT = httpx.Timeout(8.0, connect=4.0)

#: Enough for any real page; short enough that nobody can hand us a film.
MAX_BYTES = 2_000_000

#: Each hop is re-checked from scratch, so this only bounds the work.
MAX_REDIRECTS = 3

#: Rules that need Google data, history, or more than one page cannot say
#: anything truthful about a single anonymous fetch, so they are not run.
#: Silence is the correct output for a rule with nothing to look at.
SINGLE_PAGE_RULES = frozenset({
    "missing_title", "title_length", "missing_meta_description",
    "missing_h1", "multiple_h1", "thin_content", "images_missing_alt",
    "missing_viewport", "no_structured_data", "missing_organization_schema",
    "content_requires_js", "page_5xx", "redirect_chain",
    "canonical_to_other_page",
})


class PeekFailed(Exception):
    """Something the visitor should be told in plain words."""


@dataclass(frozen=True, slots=True)
class Peek:
    url: str
    final_url: str
    status_code: int
    title: str | None
    findings: list[dict[str, Any]]
    checked: int


def normalise(raw: str) -> str:
    """What somebody types into a box, turned into a URL.

    People type `example.com`. They do not type a scheme, and asking them to
    is the kind of friction that ends a first visit.
    """
    text = (raw or "").strip()
    if not text:
        raise PeekFailed("Enter a website address.")
    if len(text) > 300:
        raise PeekFailed("That address is too long.")
    if "://" not in text:
        text = f"https://{text}"

    parts = urlsplit(text)
    if not parts.hostname:
        raise PeekFailed("That does not look like a website address.")
    # One page — whichever one they named. A pasted deep link is checked
    # as given; a bare domain becomes its homepage. The query string goes:
    # it is where tracking parameters and session ids live.
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


@dataclass(frozen=True, slots=True)
class Fetched:
    url: str
    status_code: int
    content_type: str
    body: str
    redirects: list[dict[str, Any]]


async def fetch_once(url: str, *, client: httpx.AsyncClient) -> Fetched:
    """Fetch a page, re-checking safety at every hop.

    Redirects are followed by hand because each new location is a new URL
    nobody has checked yet — "302 to 169.254.169.254" is the whole reason a
    guard that only inspects the first URL is not a guard.
    """
    seen = url
    redirects: list[dict[str, Any]] = []

    for _ in range(MAX_REDIRECTS + 1):
        check_url(seen)
        request = client.build_request(
            "GET", seen, headers={"User-Agent": USER_AGENT_STRING},
        )
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            # Where the socket ACTUALLY went, before a byte of body is read.
            stream = response.extensions.get("network_stream")
            peer = stream.get_extra_info("server_addr") if stream else None
            check_peer(peer[0] if peer else None)

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise PeekFailed("That page redirected to nowhere.")
                redirects.append(
                    {"from": seen, "status": response.status_code}
                )
                seen = str(response.url.join(location))
                continue

            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise PeekFailed("That page is too large to scan.")

            return Fetched(
                url=seen,
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
                body=bytes(body).decode(
                    response.encoding or "utf-8", errors="replace"
                ),
                redirects=redirects,
            )
        finally:
            await response.aclose()

    raise PeekFailed("That address redirects too many times.")


async def run(raw: str, *, client: httpx.AsyncClient | None = None) -> Peek:
    """Normalise, fetch, extract, and run the real rules. Writes nothing."""
    url = normalise(raw)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)
    try:
        fetched = await fetch_once(url, client=client)
    finally:
        if own_client:
            await client.aclose()

    if "html" not in fetched.content_type.lower():
        raise PeekFailed("That address did not return a web page.")

    facts = extract.extract(fetched.body, url=fetched.url)
    found = findings_for(
        fetched.url, fetched.status_code, facts, fetched.redirects,
    )
    return Peek(
        url=url,
        final_url=fetched.url,
        status_code=fetched.status_code,
        title=facts.title,
        findings=[
            {
                "type": f.type_key,
                "severity": f.severity,
                "evidence": f.evidence,
            }
            for f in found
        ],
        checked=len(SINGLE_PAGE_RULES),
    )


def _page_row(
    url: str, status: int, facts: extract.PageFacts,
    redirects: list[dict[str, Any]] | None = None,
) -> PageRow:
    canonical = facts.canonical_url
    return PageRow(
        page_id=None,
        url=url,
        url_hash=hashlib.sha256(url.encode()).digest(),
        status_code=status,
        title=facts.title,
        meta_description=facts.meta_description,
        h1=facts.h1,
        word_count=facts.word_count,
        canonical_url=canonical,
        canonical_is_self=(
            None if canonical is None
            else canonical.rstrip("/") == url.rstrip("/")
        ),
        robots_meta=facts.robots_meta,
        viewport_present=facts.viewport_present,
        schema_types=facts.schema_types,
        images_total=facts.images_total,
        images_missing_alt=facts.images_missing_alt,
        # One page in isolation: we did not crawl anything that could link to
        # it, so "no inbound links" is unknown, not zero. Claiming zero would
        # make every peek report an orphan page.
        internal_outlinks=sum(1 for link in facts.links if link.internal),
        text_hash=facts.text_hash,
        redirect_chain=redirects or None,
        depth=0,
        render_mode="http",
        body_text_length=facts.body_text_length,
    )


def findings_for(
    url: str, status: int, facts: extract.PageFacts,
    redirects: list[dict[str, Any]] | None = None,
) -> list[Finding]:
    """The real rules, over a context holding exactly one page."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    context = AnalysisContext(
        website_id=None,
        origin=origin,
        pages=[_page_row(url, status, facts, redirects)],
        # Everything below is genuinely unknown from one anonymous fetch, and
        # is left empty so the rules that depend on it stay quiet.
        linked_url_hashes=set(),
        sitemap_found=True,
        robots_blocks_crawl=False,
    )

    found: list[Finding] = []
    for key, rule in all_rules():
        if key not in SINGLE_PAGE_RULES:
            continue
        try:
            found.extend(rule(context))
        except Exception:  # noqa: BLE001 — one broken rule must not lose the rest
            continue
    return found
