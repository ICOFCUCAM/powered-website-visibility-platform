"""The issue catalogue (docs/05-analysis-scoring.md).

Seeded into `issue_types` from here, so a rule and its metadata row cannot
drift apart. `weight` is the contribution to the scorecard component named by
`category`; `effort` is what the recommendation ranker uses alongside impact.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IssueType:
    key: str
    category: str
    severity: str
    title: str
    summary: str
    scope_type: str
    weight: float
    effort: str
    rule_version: str = "1.0.0"


CATALOGUE: tuple[IssueType, ...] = (
    # -- technical ---------------------------------------------------------
    IssueType("page_5xx", "technical", "critical", "Page returns a server error",
              "The server failed while returning this page, so nobody — "
              "including Google — can read it.", "page", 5, "high"),
    IssueType("page_404_internal_link", "technical", "critical",
              "Broken link on your own site",
              "A page on your site links to an address that no longer exists.",
              "page", 4, "low"),
    IssueType("robots_blocks_crawl", "technical", "critical",
              "Your robots.txt blocks everything",
              "Your robots.txt tells every crawler, including Google's, to "
              "stay out of the whole site.", "website", 5, "low"),
    IssueType("noindex_on_valuable_page", "technical", "critical",
              "A page Google sends traffic to is marked noindex",
              "This page asks Google not to list it, but people are finding "
              "you through it.", "page", 5, "low"),
    IssueType("redirect_chain", "technical", "medium", "Long redirect chain",
              "Visitors and crawlers are bounced through several addresses "
              "before reaching the page.", "page", 2, "medium"),
    IssueType("canonical_to_other_page", "technical", "high",
              "Canonical points somewhere else",
              "This page tells Google to index a different address instead, "
              "which means this one will not appear in search.", "page", 3,
              "medium"),
    IssueType("missing_viewport", "technical", "high", "No mobile viewport",
              "Without a viewport tag the page does not adapt to phones, "
              "where most searches happen.", "page", 3, "low"),
    IssueType("no_sitemap", "technical", "medium", "No sitemap found",
              "A sitemap tells Google which pages you consider real. We "
              "couldn't find one.", "website", 2, "low"),

    # -- content -----------------------------------------------------------
    IssueType("missing_title", "content", "critical", "Missing page title",
              "The title is what people click in Google. This page has none.",
              "page", 5, "low"),
    IssueType("duplicate_title", "content", "high", "Duplicate page title",
              "Several pages share one title, so people can't tell them apart "
              "in search results.", "page", 3, "low"),
    IssueType("title_length", "content", "low", "Title is too short or too long",
              "Very short titles waste the space; very long ones get cut off.",
              "page", 1, "low"),
    IssueType("missing_meta_description", "content", "high",
              "Missing meta description",
              "Google writes its own summary when you don't supply one, and it "
              "rarely says what you would.", "page", 2, "low"),
    IssueType("duplicate_meta_description", "content", "medium",
              "Duplicate meta description",
              "Several pages share one description.", "page", 2, "low"),
    IssueType("missing_h1", "content", "medium", "No main heading",
              "The page has no H1, so its subject is unclear to readers and "
              "to Google.", "page", 2, "low"),
    IssueType("multiple_h1", "content", "low", "More than one main heading",
              "Several H1s on one page muddy what it is about.", "page", 1,
              "low"),
    IssueType("thin_content", "content", "medium", "Very little content",
              "There is not enough on this page to answer anyone's question.",
              "page", 2, "high"),
    IssueType("duplicate_content", "content", "high", "Duplicate content",
              "Two pages on your site have effectively the same text.", "page",
              3, "medium"),
    IssueType("images_missing_alt", "content", "medium",
              "Images without alt text",
              "Alt text describes an image to screen readers and to Google.",
              "page", 2, "low"),
    IssueType("orphan_page", "content", "medium", "Page nothing links to",
              "Nothing on your site links here, so visitors can only arrive "
              "from search.", "page", 2, "low"),

    # -- seo / opportunity (needs Search Console) --------------------------
    IssueType("ctr_below_position_baseline", "seo", "high",
              "Good position, few clicks",
              "This page ranks well but people aren't clicking, which usually "
              "means the title or description isn't doing its job.", "page", 4,
              "low"),
    IssueType("striking_distance_keyword", "seo", "high",
              "Almost on page one",
              "You rank just below the first page for this search. A small "
              "improvement can move it up.", "keyword", 4, "medium"),
    IssueType("declining_page", "seo", "high", "Losing search traffic",
              "This page is getting noticeably fewer clicks than it was.",
              "page", 3, "medium"),
    IssueType("cannibalisation", "seo", "medium",
              "Two pages competing for one search",
              "Google keeps swapping which of your pages it shows, which "
              "weakens both.", "keyword", 2, "medium"),

    # -- AI visibility proxies --------------------------------------------
    IssueType("ai_crawler_blocked", "ai_search", "high",
              "AI assistants are blocked",
              "Your robots.txt blocks the crawlers that AI assistants use, so "
              "they cannot cite you.", "website", 4, "low"),
    IssueType("no_structured_data", "ai_search", "high",
              "No structured data",
              "Structured data tells machines what your pages are about.",
              "page", 3, "medium"),
    IssueType("missing_organization_schema", "ai_search", "medium",
              "No organisation details in structured data",
              "Nothing on your homepage tells a machine who you are.", "website",
              2, "low"),
    IssueType("content_requires_js", "ai_search", "high",
              "Content only appears with JavaScript",
              "The words are not in the page itself, so tools that don't run "
              "JavaScript see an empty page.", "page", 3, "high"),
)

BY_KEY = {t.key: t for t in CATALOGUE}


async def seed(conn) -> int:
    """Idempotent. Run on deploy: a rule whose row is missing would produce
    issues with no title, severity or weight."""
    async with conn.cursor() as cur:
        await cur.executemany(
            """
            insert into issue_types
                (key, category, default_severity, title, summary, scope_type,
                 score_weight, effort, rule_version, active)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, true)
            on conflict (key) do update
               set category = excluded.category,
                   default_severity = excluded.default_severity,
                   title = excluded.title,
                   summary = excluded.summary,
                   scope_type = excluded.scope_type,
                   score_weight = excluded.score_weight,
                   effort = excluded.effort,
                   rule_version = excluded.rule_version,
                   active = true
            """,
            [
                (t.key, t.category, t.severity, t.title, t.summary,
                 t.scope_type, t.weight, t.effort, t.rule_version)
                for t in CATALOGUE
            ],
        )
    return len(CATALOGUE)
