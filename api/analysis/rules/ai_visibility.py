"""AI visibility proxies.

Honest proxies for machine readability, computable on every crawl. They are
labelled as MODELLED, not measured: whether an AI assistant actually cites the
site is a v3 question with its own cost model, and claiming to know it from a
crawl would be exactly the kind of invented metric this product refuses.
"""

from __future__ import annotations

from collections.abc import Iterable

from api.analysis.fingerprint import page_scope
from api.analysis.rules.base import AnalysisContext, Finding, rule

#: Below this, with the page having rendered differently, the words are not in
#: the HTML — which is what a tool that does not run JavaScript sees.
JS_DEPENDENT_TEXT_LENGTH = 200

ORGANISATION_TYPES = {"Organization", "LocalBusiness", "Corporation", "NGO",
                      "Church", "NonprofitOrganization"}


@rule("ai_crawler_blocked")
def ai_crawler_blocked(ctx: AnalysisContext) -> Iterable[Finding]:
    if ctx.ai_crawlers_blocked:
        yield Finding(
            "ai_crawler_blocked", "website", None,
            {"blocked": sorted(ctx.ai_crawlers_blocked), "origin": ctx.origin},
        )


@rule("no_structured_data")
def no_structured_data(ctx: AnalysisContext) -> Iterable[Finding]:
    for page in ctx.pages:
        if not page.is_ok or not page.is_indexable:
            continue
        if not page.schema_types:
            yield Finding(
                "no_structured_data", "page", page_scope(page.url_hash),
                {"url": page.url}, page_id=page.page_id,
            )


@rule("missing_organization_schema")
def missing_organization_schema(ctx: AnalysisContext) -> Iterable[Finding]:
    """Site-wide, judged on the homepage: nothing tells a machine who you are."""
    homepage = next((p for p in ctx.pages if p.depth == 0), None)
    if homepage is None:
        return
    if not (set(homepage.schema_types) & ORGANISATION_TYPES):
        yield Finding(
            "missing_organization_schema", "website", None,
            {"url": homepage.url, "found_types": sorted(set(homepage.schema_types))},
        )


@rule("content_requires_js")
def content_requires_js(ctx: AnalysisContext) -> Iterable[Finding]:
    """The page had almost no text until a browser ran its JavaScript."""
    for page in ctx.pages:
        if not page.is_ok:
            continue
        rendered_and_thin = (
            page.render_mode == "browser"
            and page.body_text_length < JS_DEPENDENT_TEXT_LENGTH
        )
        if rendered_and_thin:
            continue
        if page.render_mode == "http" and page.word_count < 30 and page.is_indexable:
            yield Finding(
                "content_requires_js", "page", page_scope(page.url_hash),
                {"url": page.url, "words_in_html": page.word_count},
                page_id=page.page_id,
            )
