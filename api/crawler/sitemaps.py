"""Sitemap discovery and parsing.

A sitemap is the site owner telling us which pages they consider real. It is
a far better seed than crawling outward from the homepage, and it is the only
way to find pages nothing links to — which is itself a finding.
"""

from __future__ import annotations

import contextlib
import gzip
import re
from dataclasses import dataclass, field

#: Bounded because a sitemap index can point at thousands of files, and a
#: 500-page crawl does not need them all.
MAX_SITEMAP_FILES = 50
MAX_URLS = 50_000
MAX_DEPTH = 3

CONVENTIONAL_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")

_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>",
                  re.IGNORECASE | re.DOTALL)
_IS_INDEX = re.compile(r"<sitemapindex", re.IGNORECASE)


@dataclass(slots=True)
class SitemapResult:
    urls: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    truncated: bool = False


def decode(body: bytes, url: str) -> str:
    """Sitemaps are commonly gzipped, and sometimes served without the header
    saying so."""
    if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
        # A mislabelled file is common enough that failing to gunzip is not
        # worth abandoning the sitemap over.
        with contextlib.suppress(OSError):
            body = gzip.decompress(body)
    return body.decode("utf-8", errors="replace")


def is_index(text: str) -> bool:
    return bool(_IS_INDEX.search(text))


def extract_locations(text: str) -> list[str]:
    return [m.group(1).strip() for m in _LOC.finditer(text) if m.group(1).strip()]
