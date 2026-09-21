"""Technical rules — can Google reach and read the page at all."""

from __future__ import annotations

from collections.abc import Iterable

from api.analysis.fingerprint import page_scope
from api.analysis.rules.base import AnalysisContext, Finding, rule

#: A redirect or two is normal. Three is a chain worth mentioning.
MAX_REASONABLE_REDIRECTS = 2


@rule("page_5xx")
def page_5xx(ctx: AnalysisContext) -> Iterable[Finding]:
    for page in ctx.pages:
        if page.status_code and page.status_code >= 500:
            yield Finding(
                "page_5xx", "page", page_scope(page.url_hash),
                {"url": page.url, "status_code": page.status_code},
                page_id=page.page_id,
                # A broken page that Google sends traffic to is worse than a
                # broken page nobody visits, and the ranking should say so.
                impact=_lost_clicks(ctx, page),
            )


@rule("robots_blocks_crawl")
def robots_blocks_crawl(ctx: AnalysisContext) -> Iterable[Finding]:
    if ctx.robots_blocks_crawl:
        yield Finding(
            "robots_blocks_crawl", "website", None,
            {"origin": ctx.origin},
            impact=float(sum(p.clicks for p in ctx.page_performance.values())),
        )


@rule("noindex_on_valuable_page")
def noindex_on_valuable_page(ctx: AnalysisContext) -> Iterable[Finding]:
    """A page asking not to be indexed, that Google is sending people to.

    Almost always a mistake left behind from a staging site, and it is
    invisible until someone looks — which is the point of this product.
    """
    for page in ctx.pages:
        if page.is_indexable:
            continue
        performance = ctx.performance_for(page)
        if performance and performance.impressions > 0:
            yield Finding(
                "noindex_on_valuable_page", "page", page_scope(page.url_hash),
                {
                    "url": page.url,
                    "robots_meta": page.robots_meta,
                    "impressions": performance.impressions,
                    "clicks": performance.clicks,
                },
                page_id=page.page_id,
                impact=float(performance.clicks),
            )


@rule("missing_viewport")
def missing_viewport(ctx: AnalysisContext) -> Iterable[Finding]:
    for page in ctx.pages:
        if page.is_ok and page.viewport_present is False:
            yield Finding(
                "missing_viewport", "page", page_scope(page.url_hash),
                {"url": page.url},
                page_id=page.page_id,
                impact=_lost_clicks(ctx, page) * 0.3,
            )


@rule("redirect_chain")
def redirect_chain(ctx: AnalysisContext) -> Iterable[Finding]:
    for page in ctx.pages:
        chain = page.redirect_chain or []
        if len(chain) > MAX_REASONABLE_REDIRECTS:
            yield Finding(
                "redirect_chain", "page", page_scope(page.url_hash),
                {"url": page.url, "hops": len(chain), "chain": chain[:5]},
                page_id=page.page_id,
            )


@rule("canonical_to_other_page")
def canonical_to_other_page(ctx: AnalysisContext) -> Iterable[Finding]:
    """A page pointing its canonical elsewhere while earning impressions.

    Not a finding on its own — canonicalising duplicates is correct. It is a
    finding when Google is showing the page anyway, because the two signals
    contradict each other.
    """
    for page in ctx.pages:
        if page.canonical_is_self is not False or not page.canonical_url:
            continue
        performance = ctx.performance_for(page)
        if performance and performance.impressions > 0:
            yield Finding(
                "canonical_to_other_page", "page", page_scope(page.url_hash),
                {
                    "url": page.url,
                    "canonical_url": page.canonical_url,
                    "impressions": performance.impressions,
                },
                page_id=page.page_id,
                impact=float(performance.clicks) * 0.5,
            )


@rule("no_sitemap")
def no_sitemap(ctx: AnalysisContext) -> Iterable[Finding]:
    if not ctx.sitemap_found:
        yield Finding("no_sitemap", "website", None, {"origin": ctx.origin})


@rule("page_404_internal_link")
def page_404_internal_link(ctx: AnalysisContext) -> Iterable[Finding]:
    for url_hash, url in ctx.broken_link_targets.items():
        yield Finding(
            "page_404_internal_link", "page", page_scope(url_hash),
            {"url": url},
        )


def _lost_clicks(ctx: AnalysisContext, page) -> float:
    performance = ctx.performance_for(page)
    return float(performance.clicks) if performance else 0.0
