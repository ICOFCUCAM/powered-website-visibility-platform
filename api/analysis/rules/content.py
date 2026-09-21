"""Content rules — is there something here worth showing."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from api.analysis.fingerprint import page_scope
from api.analysis.rules.base import AnalysisContext, Finding, PageRow, rule

TITLE_MIN = 30
TITLE_MAX = 60
THIN_CONTENT_WORDS = 200
ALT_COVERAGE_THRESHOLD = 0.8


def _indexable(ctx: AnalysisContext) -> list[PageRow]:
    """Rules apply to pages Google would actually list.

    Flagging a missing title on a noindex page is technically true and
    practically noise.
    """
    return [p for p in ctx.pages if p.is_ok and p.is_indexable]


@rule("missing_title")
def missing_title(ctx: AnalysisContext) -> Iterable[Finding]:
    for page in _indexable(ctx):
        if not (page.title or "").strip():
            yield Finding(
                "missing_title", "page", page_scope(page.url_hash),
                {"url": page.url},
                page_id=page.page_id,
                impact=_impressions(ctx, page) * 0.05,
            )


@rule("title_length")
def title_length(ctx: AnalysisContext) -> Iterable[Finding]:
    for page in _indexable(ctx):
        title = (page.title or "").strip()
        if not title:
            continue
        if len(title) < TITLE_MIN or len(title) > TITLE_MAX:
            yield Finding(
                "title_length", "page", page_scope(page.url_hash),
                {
                    "url": page.url,
                    "title": title,
                    "length": len(title),
                    "recommended": f"{TITLE_MIN}-{TITLE_MAX}",
                },
                page_id=page.page_id,
            )


@rule("duplicate_title")
def duplicate_title(ctx: AnalysisContext) -> Iterable[Finding]:
    yield from _duplicates(
        ctx, "duplicate_title", lambda p: (p.title or "").strip().lower() or None
    )


@rule("missing_meta_description")
def missing_meta_description(ctx: AnalysisContext) -> Iterable[Finding]:
    for page in _indexable(ctx):
        if not (page.meta_description or "").strip():
            yield Finding(
                "missing_meta_description", "page", page_scope(page.url_hash),
                {"url": page.url},
                page_id=page.page_id,
                impact=_impressions(ctx, page) * 0.01,
            )


@rule("duplicate_meta_description")
def duplicate_meta_description(ctx: AnalysisContext) -> Iterable[Finding]:
    yield from _duplicates(
        ctx,
        "duplicate_meta_description",
        lambda p: (p.meta_description or "").strip().lower() or None,
    )


@rule("missing_h1")
def missing_h1(ctx: AnalysisContext) -> Iterable[Finding]:
    for page in _indexable(ctx):
        if not page.h1:
            yield Finding(
                "missing_h1", "page", page_scope(page.url_hash),
                {"url": page.url}, page_id=page.page_id,
            )


@rule("multiple_h1")
def multiple_h1(ctx: AnalysisContext) -> Iterable[Finding]:
    for page in _indexable(ctx):
        if len(page.h1) > 1:
            yield Finding(
                "multiple_h1", "page", page_scope(page.url_hash),
                {"url": page.url, "count": len(page.h1), "headings": page.h1[:5]},
                page_id=page.page_id,
            )


@rule("thin_content")
def thin_content(ctx: AnalysisContext) -> Iterable[Finding]:
    for page in _indexable(ctx):
        if page.word_count < THIN_CONTENT_WORDS:
            yield Finding(
                "thin_content", "page", page_scope(page.url_hash),
                {"url": page.url, "word_count": page.word_count,
                 "threshold": THIN_CONTENT_WORDS},
                page_id=page.page_id,
            )


@rule("duplicate_content")
def duplicate_content(ctx: AnalysisContext) -> Iterable[Finding]:
    """Same text on two URLs.

    Pages that canonicalise to each other are excluded: that is the correct
    way to have duplicates, and flagging it would punish doing the right thing.
    """
    by_hash: dict[bytes, list[PageRow]] = defaultdict(list)
    for page in _indexable(ctx):
        if page.text_hash and page.word_count >= THIN_CONTENT_WORDS:
            by_hash[page.text_hash].append(page)

    for pages in by_hash.values():
        if len(pages) < 2:
            continue
        canonicals = {p.canonical_url for p in pages if p.canonical_url}
        if len(canonicals) == 1 and len(pages) > 1:
            continue  # correctly canonicalised
        others = [p.url for p in pages]
        for page in pages:
            yield Finding(
                "duplicate_content", "page", page_scope(page.url_hash),
                {"url": page.url,
                 "duplicates": [u for u in others if u != page.url][:5]},
                page_id=page.page_id,
            )


@rule("images_missing_alt")
def images_missing_alt(ctx: AnalysisContext) -> Iterable[Finding]:
    for page in _indexable(ctx):
        if page.images_total <= 0:
            continue
        coverage = 1 - (page.images_missing_alt / page.images_total)
        if coverage < ALT_COVERAGE_THRESHOLD:
            yield Finding(
                "images_missing_alt", "page", page_scope(page.url_hash),
                {
                    "url": page.url,
                    "images": page.images_total,
                    "missing_alt": page.images_missing_alt,
                    "coverage": round(coverage, 2),
                },
                page_id=page.page_id,
            )


@rule("orphan_page")
def orphan_page(ctx: AnalysisContext) -> Iterable[Finding]:
    """A page nothing links to.

    Only meaningful once the crawl found links at all; on a crawl that fetched
    one page, everything looks orphaned.
    """
    if len(ctx.pages) < 2 or not ctx.linked_url_hashes:
        return
    for page in _indexable(ctx):
        if page.depth == 0:
            continue  # the homepage is nobody's orphan
        if page.url_hash not in ctx.linked_url_hashes:
            yield Finding(
                "orphan_page", "page", page_scope(page.url_hash),
                {"url": page.url}, page_id=page.page_id,
            )


def _duplicates(ctx: AnalysisContext, key: str, extract) -> Iterable[Finding]:
    groups: dict[str, list[PageRow]] = defaultdict(list)
    for page in _indexable(ctx):
        value = extract(page)
        if value:
            groups[value].append(page)

    for value, pages in groups.items():
        if len(pages) < 2:
            continue
        for page in pages:
            yield Finding(
                key, "page", page_scope(page.url_hash),
                {
                    "url": page.url,
                    "value": value[:200],
                    "shared_with": [p.url for p in pages if p.url != page.url][:5],
                    "count": len(pages),
                },
                page_id=page.page_id,
            )


def _impressions(ctx: AnalysisContext, page: PageRow) -> float:
    performance = ctx.performance_for(page)
    return float(performance.impressions) if performance else 0.0
