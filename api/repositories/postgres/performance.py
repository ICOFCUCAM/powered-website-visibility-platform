"""Reading Search Console performance.

Core-side: reads the normalised tables the Hub writes, and never constructs a
Google client. That is the boundary that makes the dashboard survive a Google
outage — the read path serves the last sync and says when it was.

Every rollup weights `position` by impressions. `avg(position)` is wrong in a
way that looks entirely plausible: position 3 on 1,000 impressions and
position 20 on 10 averages to 11.5, when the site is effectively at 3.2.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one


@dataclass(frozen=True, slots=True)
class Totals:
    clicks: int
    impressions: int
    ctr: float | None
    position: float | None

    @classmethod
    def of(cls, row: dict[str, Any] | None) -> Totals:
        if not row or not row.get("impressions"):
            return cls(0, 0, None, None)
        return cls(
            clicks=int(row["clicks"] or 0),
            impressions=int(row["impressions"] or 0),
            ctr=float(row["ctr"]) if row["ctr"] is not None else None,
            position=float(row["position"]) if row["position"] is not None else None,
        )


class PerformanceRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def totals(
        self, website_id: UUID, start: date, end: date
    ) -> dict[str, Any] | None:
        """From the RECONCILIATION AUTHORITY, never from summed query rows."""
        return await fetch_one(
            self._conn,
            """
            select coalesce(sum(clicks), 0)      as clicks,
                   coalesce(sum(impressions), 0) as impressions,
                   case when sum(impressions) > 0
                        then sum(clicks)::numeric / sum(impressions) end as ctr,
                   case when sum(impressions) > 0
                        then sum(position * impressions) / sum(impressions) end
                                                 as position
              from gsc_totals_daily
             where website_id = %s and date between %s and %s
            """,
            (website_id, start, end),
        )

    async def daily_series(
        self, website_id: UUID, start: date, end: date
    ) -> list[dict[str, Any]]:
        return await fetch_all(
            self._conn,
            """
            select date, clicks, impressions,
                   case when impressions > 0
                        then clicks::numeric / impressions end as ctr,
                   position
              from gsc_totals_daily
             where website_id = %s and date between %s and %s
             order by date
            """,
            (website_id, start, end),
        )

    async def anonymised_share(
        self, website_id: UUID, start: date, end: date
    ) -> dict[str, Any] | None:
        """How many clicks Google withheld from the query dimension.

        `has_query_data` matters more than it looks. The gap is computed as
        site totals minus what the query rows account for — so before the
        query sync has run, EVERY click looks anonymised. Reporting that would
        tell a customer Google withheld all their data when in fact we simply
        had not fetched it yet. Absence of data is not anonymisation.
        """
        return await fetch_one(
            self._conn,
            """
            select coalesce(sum(a.total_clicks), 0)      as total_clicks,
                   coalesce(sum(a.attributed_clicks), 0) as attributed_clicks,
                   coalesce(sum(a.anonymised_clicks), 0) as anonymised_clicks,
                   case when sum(a.total_clicks) > 0
                        then sum(a.anonymised_clicks)::numeric / sum(a.total_clicks)
                        end                              as anonymised_share,
                   exists(
                       select 1 from gsc_query_daily q
                        where q.website_id = %s and q.date between %s and %s
                   )                                     as has_query_data
              from gsc_anonymised_share a
             where a.website_id = %s and a.date between %s and %s
            """,
            (website_id, start, end, website_id, start, end),
        )

    async def top_queries(
        self,
        website_id: UUID,
        start: date,
        end: date,
        *,
        order_by: str = "clicks",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._top(
            "gsc_query_daily", "query_hash", "query", website_id, start, end,
            order_by, limit,
        )

    async def top_pages(
        self,
        website_id: UUID,
        start: date,
        end: date,
        *,
        order_by: str = "clicks",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._top(
            "gsc_page_daily", "url_hash", "url", website_id, start, end,
            order_by, limit,
        )

    _ORDERABLE = {
        "clicks": "clicks desc",
        "impressions": "impressions desc",
        "ctr": "ctr desc nulls last",
        # Ascending: position 1 is the best, so "order by position" meaning
        # "worst first" would surprise every user who has ever used Search
        # Console.
        "position": "position asc nulls last",
    }

    async def _top(
        self,
        table: str,
        key_column: str,
        label_column: str,
        website_id: UUID,
        start: date,
        end: date,
        order_by: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        # Allowlisted, never interpolated from user input.
        ordering = self._ORDERABLE.get(order_by, self._ORDERABLE["clicks"])
        sql = f"""
            select min({label_column}) as label,
                   sum(clicks)         as clicks,
                   sum(impressions)    as impressions,
                   case when sum(impressions) > 0
                        then sum(clicks)::numeric / sum(impressions) end as ctr,
                   case when sum(impressions) > 0
                        then sum(position * impressions) / sum(impressions) end
                                       as position
              from {table}
             where website_id = %s and date between %s and %s
             group by {key_column}
             order by {ordering}
             limit %s
        """
        return await fetch_all(self._conn, sql, (website_id, start, end, limit))


class AnalyticsRepository:
    """Reading GA4 facts.

    Outcomes are reported only where the customer mapped a goal event. Where
    they have not, the answer is "not configured" — never a conversion rate
    assembled from whichever event happened to look important.
    """

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def totals(
        self, website_id: UUID, start: date, end: date
    ) -> dict[str, Any] | None:
        return await fetch_one(
            self._conn,
            """
            select coalesce(sum(sessions), 0)         as sessions,
                   coalesce(sum(active_users), 0)     as active_users,
                   coalesce(sum(engaged_sessions), 0) as engaged_sessions,
                   case when sum(sessions) > 0
                        then sum(engaged_sessions)::numeric / sum(sessions) end
                                                      as engagement_rate,
                   coalesce(sum(key_events), 0)       as key_events
              from ga4_daily
             where website_id = %s and date between %s and %s
            """,
            (website_id, start, end),
        )

    async def daily_series(
        self, website_id: UUID, start: date, end: date
    ) -> list[dict[str, Any]]:
        return await fetch_all(
            self._conn,
            """
            select date, sessions, active_users, engaged_sessions, key_events
              from ga4_daily
             where website_id = %s and date between %s and %s
             order by date
            """,
            (website_id, start, end),
        )

    async def by_dimension(
        self, website_id: UUID, dimension_type: str, start: date, end: date,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return await fetch_all(
            self._conn,
            """
            select dimension_value as label,
                   sum(sessions)      as sessions,
                   sum(active_users)  as active_users,
                   sum(key_events)    as key_events
              from ga4_dimension_daily
             where website_id = %s and dimension_type = %s
               and date between %s and %s
             group by dimension_value
             order by sessions desc
             limit %s
            """,
            (website_id, dimension_type, start, end, limit),
        )

    async def outcomes(
        self, website_id: UUID, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Counts for mapped goals only. An unmapped property returns [], and
        the caller must render that as "not configured" rather than zero."""
        return await fetch_all(
            self._conn,
            """
            select g.event_name, min(e.label) as label, min(e.goal_kind) as goal_kind,
                   sum(g.event_count) as count
              from ga4_goal_daily g
              join ga4_goal_events e
                on e.website_id = g.website_id and e.event_name = g.event_name
             where g.website_id = %s and g.date between %s and %s
             group by g.event_name
             order by count desc
            """,
            (website_id, start, end),
        )

    async def has_goals(self, website_id: UUID) -> bool:
        row = await fetch_one(
            self._conn,
            "select exists(select 1 from ga4_goal_events where website_id = %s) as ok",
            (website_id,),
        )
        return bool(row and row["ok"])
