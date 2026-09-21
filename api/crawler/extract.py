"""HTML extraction.

Produces the facts migration 0004 stores and migration 0005's rules judge.
Deliberately dumb: it records what is on the page and forms no opinion. A
missing title is recorded as a missing title, not as an issue — detection is
a separate, versioned step, and mixing them would make a rule change require
a re-crawl.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser

#: An image this size is worth flagging whatever the page is.
OVERSIZED_IMAGE_BYTES = 500_000

_WHITESPACE = re.compile(r"\s+")


@dataclass(slots=True)
class Link:
    url: str
    anchor_text: str
    rel: list[str]
    is_internal: bool


@dataclass(slots=True)
class PageFacts:
    """Everything the crawler saw. Versioned as `extract_version`, so a future
    index can replay extraction over stored HTML without re-fetching."""

    title: str | None = None
    meta_description: str | None = None
    h1: list[str] = field(default_factory=list)
    heading_counts: dict[str, int] = field(default_factory=dict)
    word_count: int = 0
    lang: str | None = None
    canonical_url: str | None = None
    robots_meta: list[str] = field(default_factory=list)
    hreflang: list[dict[str, str]] = field(default_factory=list)
    viewport_present: bool = False
    schema_types: list[str] = field(default_factory=list)
    schema_errors: list[str] = field(default_factory=list)
    open_graph: dict[str, str] = field(default_factory=dict)
    images_total: int = 0
    images_missing_alt: int = 0
    links: list[Link] = field(default_factory=list)
    text_hash: bytes | None = None
    body_text_length: int = 0
    script_bytes: int = 0
    has_spa_root: bool = False

    @property
    def title_length(self) -> int | None:
        return len(self.title) if self.title else None

    @property
    def internal_links(self) -> list[Link]:
        return [link for link in self.links if link.is_internal]

    @property
    def external_links(self) -> list[Link]:
        return [link for link in self.links if not link.is_internal]


def _text(node) -> str:
    return _WHITESPACE.sub(" ", node.text(separator=" ", strip=True) or "").strip()


def extract(html: str, *, url: str) -> PageFacts:
    tree = HTMLParser(html)
    facts = PageFacts()
    host = (urlsplit(url).hostname or "").lower()

    if title := tree.css_first("title"):
        facts.title = _text(title) or None

    for meta in tree.css("meta"):
        name = (meta.attributes.get("name") or "").lower()
        prop = (meta.attributes.get("property") or "").lower()
        content = meta.attributes.get("content") or ""
        if name == "description":
            facts.meta_description = content.strip() or None
        elif name == "robots":
            facts.robots_meta = [
                d.strip().lower() for d in content.split(",") if d.strip()
            ]
        elif name == "viewport":
            facts.viewport_present = True
        elif prop.startswith("og:"):
            facts.open_graph[prop] = content

    if html_tag := tree.css_first("html"):
        facts.lang = (html_tag.attributes.get("lang") or "").strip() or None

    for link in tree.css("link"):
        rel = (link.attributes.get("rel") or "").lower()
        href = link.attributes.get("href")
        if not href:
            continue
        if rel == "canonical":
            facts.canonical_url = urljoin(url, href)
        elif rel == "alternate" and link.attributes.get("hreflang"):
            facts.hreflang.append(
                {"hreflang": link.attributes["hreflang"], "href": urljoin(url, href)}
            )

    for level in range(1, 7):
        nodes = tree.css(f"h{level}")
        if nodes:
            facts.heading_counts[f"h{level}"] = len(nodes)
        if level == 1:
            facts.h1 = [_text(n) for n in nodes if _text(n)]

    for img in tree.css("img"):
        facts.images_total += 1
        # A missing alt and an empty alt are different: empty is a valid way
        # to mark an image decorative, absent is an omission. The parser
        # reports an empty value as None, so presence is tested on the key.
        if "alt" not in img.attributes:
            facts.images_missing_alt += 1

    for script in tree.css('script[type="application/ld+json"]'):
        raw = script.text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Recorded rather than raised: broken structured data is a finding
            # for the customer, not a crawl failure.
            facts.schema_errors.append(f"invalid JSON-LD: {exc.msg}")
            continue
        facts.schema_types.extend(_schema_types(data))

    for anchor in tree.css("a"):
        href = anchor.attributes.get("href")
        if not href:
            continue
        absolute = urljoin(url, href)
        # The fragment never reaches the server: /a#top and /a are one page.
        absolute = urlsplit(absolute)._replace(fragment="").geturl()
        link_host = (urlsplit(absolute).hostname or "").lower()
        rel_value = (anchor.attributes.get("rel") or "").lower()
        facts.links.append(
            Link(
                url=absolute,
                anchor_text=_text(anchor)[:300],
                rel=[r for r in rel_value.split() if r],
                is_internal=bool(link_host) and _same_site(host, link_host),
            )
        )

    for script in tree.css("script"):
        facts.script_bytes += len(script.text() or "")
        if src := script.attributes.get("src"):
            facts.script_bytes += len(src)

    # Measured before stripping, because inline script is exactly what the
    # render heuristic weighs — and counted out of the body text afterwards,
    # because a page is not "long" for carrying 60 KB of JavaScript.
    tree.strip_tags(["script", "style", "noscript", "template"])

    body = tree.css_first("body")
    body_text = _text(body) if body else ""
    facts.body_text_length = len(body_text)
    facts.word_count = len(body_text.split()) if body_text else 0
    facts.text_hash = hashlib.sha256(body_text.encode("utf-8")).digest()

    facts.has_spa_root = any(
        tree.css_first(selector) is not None
        for selector in ("#root", "#app", "#__next", "app-root")
    )

    return facts


def _same_site(page_host: str, link_host: str) -> bool:
    bare = page_host[4:] if page_host.startswith("www.") else page_host
    return link_host in (page_host, bare, f"www.{bare}")


def _schema_types(data) -> list[str]:
    """JSON-LD nests: a @graph holds many objects, and @type can be a list."""
    found: list[str] = []
    if isinstance(data, list):
        for item in data:
            found.extend(_schema_types(item))
    elif isinstance(data, dict):
        node_type = data.get("@type")
        if isinstance(node_type, str):
            found.append(node_type)
        elif isinstance(node_type, list):
            found.extend(t for t in node_type if isinstance(t, str))
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in data:
                found.extend(_schema_types(data[key]))
    return found
