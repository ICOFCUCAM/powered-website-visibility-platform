"""The home screen's data, in one query set.

Assembled server-side on purpose (V1 spec s30): a dashboard that makes nine
round trips is slow on the connection that matters most — a phone, on mobile
data, in the first minute someone uses the product.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one
from api.analysis.scoring import SCORING_VERSION

#: How the attention panel groups findings, as (singular, plural).
#:
#: Written as whole sentences rather than assembled from fragments, because
#: verb agreement does not survive a `{n} page{s} have` template: "1 page have
#: no title" is the kind of small wrongness that makes a product feel unfinished.
OPPORTUNITY_PHRASES: dict[str, tuple[str, str]] = {
    "ctr_below_position_baseline": (
        "1 page gets impressions but few clicks",
        "{n} pages get impressions but few clicks",
    ),
    "striking_distance_keyword": (
        "1 search sits just below page one",
        "{n} searches sit just below page one",
    ),
    "missing_meta_description": (
        "1 page has no description for Google to show",
        "{n} pages have no description for Google to show",
    ),
    "missing_title": ("1 page has no title", "{n} pages have no title"),
    "duplicate_title": (
        "1 page shares its title with another page",
        "{n} pages share a title with another page",
    ),
    "thin_content": (
        "1 page has very little content",
        "{n} pages have very little content",
    ),
    "declining_page": (
        "1 page is losing search traffic",
        "{n} pages are losing search traffic",
    ),
    "no_structured_data": (
        "1 page has no structured data",
        "{n} pages have no structured data",
    ),
    "images_missing_alt": (
        "1 page has images without alt text",
        "{n} pages have images without alt text",
    ),
    "orphan_page": (
        "1 page has nothing linking to it",
        "{n} pages have nothing linking to them",
    ),
    "missing_h1": ("1 page has no main heading", "{n} pages have no main heading"),
    "multiple_h1": (
        "1 page has more than one main heading",
        "{n} pages have more than one main heading",
    ),
    "duplicate_content": (
        "1 page duplicates another page's text",
        "{n} pages duplicate another page's text",
    ),
    "page_5xx": ("1 page returns a server error", "{n} pages return a server error"),
    "page_404_internal_link": (
        "1 broken link on your own site",
        "{n} broken links on your own site",
    ),
    "missing_viewport": (
        "1 page doesn't adapt to phones",
        "{n} pages don't adapt to phones",
    ),
    "noindex_on_valuable_page": (
        "1 page Google sends traffic to is hidden from search",
        "{n} pages Google sends traffic to are hidden from search",
    ),
}


def pct_change(now: Any, before: Any) -> float | None:
    """Movement as a percentage, or None when there is nothing to compare to.

    Lives here rather than in the handler because the weekly email quotes the
    same figures as the screen, and "the email disagrees with the dashboard"
    is the kind of bug a customer reports once and never trusts you about
    again. One definition, two readers.
    """
    if now is None or not before:
        return None
    return round((float(now) - float(before)) / float(before) * 100, 1)


@dataclass(frozen=True, slots=True)
class Opportunity:
    type_key: str
    headline: str
    count: int
    impact: float
    severity: str
    category: str


def phrase_for(type_key: str, count: int, fallback: str) -> str:
    """The sentence a customer reads.

    Falls back to the catalogue title with a count, which is never wrong even
    if it is never elegant — a missing phrase should read plainly, not
    ungrammatically.
    """
    forms = OPPORTUNITY_PHRASES.get(type_key)
    if forms is None:
        return fallback if count == 1 else f"{count} × {fallback}"
    singular, plural = forms
    return singular if count == 1 else plural.format(n=count)


class DashboardRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def latest_score(self, website_id: UUID) -> dict[str, Any] | None:
        return await fetch_one(
            self._conn,
            """
            select as_of, total, technical_health, search_performance,
                   content_health, analytics_coverage, ai_visibility,
                   components, scoring_version
              from score_snapshots
             where website_id = %s and scoring_version = %s
             order by as_of desc limit 1
            """,
            (website_id, SCORING_VERSION),
        )

    async def score_before(
        self, website_id: UUID, before: date
    ) -> dict[str, Any] | None:
        """The most recent snapshot at least a month older.

        Compared against a real earlier measurement rather than an arbitrary
        window, so "up 8% this month" means a number that actually existed
        then.
        """
        return await fetch_one(
            self._conn,
            """
            select as_of, total from score_snapshots
             where website_id = %s and scoring_version = %s and as_of <= %s
             order by as_of desc limit 1
            """,
            (website_id, SCORING_VERSION, before),
        )

    async def score_history(
        self, website_id: UUID, limit: int = 180
    ) -> list[dict[str, Any]]:
        rows = await fetch_all(
            self._conn,
            """
            select as_of, total, technical_health, search_performance,
                   content_health, analytics_coverage
              from score_snapshots
             where website_id = %s and scoring_version = %s
             order by as_of desc limit %s
            """,
            (website_id, SCORING_VERSION, limit),
        )
        return list(reversed(rows))

    async def attention(self, website_id: UUID) -> list[dict[str, Any]]:
        return await fetch_all(
            self._conn,
            """
            select i.type_key, i.severity, t.category, t.title,
                   count(*) as n, sum(i.impact_score) as impact
              from issues i join issue_types t on t.key = i.type_key
             where i.website_id = %s and i.status in ('open','regressed')
             group by i.type_key, i.severity, t.category, t.title
             order by sum(i.impact_score) desc, count(*) desc
            """,
            (website_id,),
        )

    async def rising_keywords(
        self, website_id: UUID, start: date, end: date,
        prior_start: date, prior_end: date, limit: int = 50,
    ) -> int:
        """Searches gaining impressions. The one green number on the panel.

        A dashboard that only reports problems is exhausting, and a customer
        who is doing well deserves to be told.
        """
        row = await fetch_one(
            self._conn,
            """
            with now as (
                select query_hash, sum(impressions) as impressions
                  from gsc_query_daily
                 where website_id = %s and date between %s and %s
                 group by query_hash),
            before as (
                select query_hash, sum(impressions) as impressions
                  from gsc_query_daily
                 where website_id = %s and date between %s and %s
                 group by query_hash)
            select count(*) as n
              from now join before using (query_hash)
             where before.impressions >= 20
               and now.impressions > before.impressions * 1.5
            """,
            (website_id, start, end, website_id, prior_start, prior_end),
        )
        return int(row["n"]) if row else 0

    async def recent_changes(
        self, website_id: UUID, limit: int = 10
    ) -> list[dict[str, Any]]:
        """The loop made visible: what was fixed, what came back, what is new.

        This is the strip that brings people back — a product that only ever
        shows a list of problems has nothing to say about progress.
        """
        return await fetch_all(
            self._conn,
            """
            select o.observed_at, o.present, i.type_key, i.status,
                   t.title, p.url
              from issue_observations o
              join issues i on i.id = o.issue_id
              join issue_types t on t.key = i.type_key
              left join pages p on p.id = i.page_id
             where o.website_id = %s
               and (o.present = false or i.status in ('regressed','verified'))
             order by o.observed_at desc
             limit %s
            """,
            (website_id, limit),
        )

    async def changes_since(
        self, website_id: UUID, since: date, limit: int = 20
    ) -> list[dict[str, Any]]:
        """The same strip as `recent_changes`, bounded by a date.

        The weekly email reports the week, not the last ten things that
        happened — on a quiet site those are months old, and presenting them
        as this week's progress would be a lie told by a date filter's
        absence.
        """
        return await fetch_all(
            self._conn,
            """
            select o.observed_at, o.present, i.type_key, i.status,
                   t.title, p.url
              from issue_observations o
              join issues i on i.id = o.issue_id
              join issue_types t on t.key = i.type_key
              left join pages p on p.id = i.page_id
             where o.website_id = %s
               and o.observed_at >= %s
               and (o.present = false or i.status in ('regressed','verified'))
             order by o.observed_at desc
             limit %s
            """,
            (website_id, since, limit),
        )

    async def last_crawl(self, website_id: UUID) -> dict[str, Any] | None:
        return await fetch_one(
            self._conn,
            """
            select id, status, finished_at, pages_fetched, error_summary
              from crawls
             where website_id = %s and status in ('completed','failed','cancelled')
             order by queued_at desc limit 1
            """,
            (website_id,),
        )

    async def last_sync(self, website_id: UUID) -> dict[str, Any] | None:
        return await fetch_one(
            self._conn,
            """
            select service, status, finished_at, range_end
              from sync_runs
             where website_id = %s and status in ('succeeded','partial')
             order by started_at desc limit 1
            """,
            (website_id,),
        )
