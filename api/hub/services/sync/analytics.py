"""GA4 synchronisation.

Answers "how is my website performing?", not "can this replace Google
Analytics?" — so it fetches a deliberately small set of reports rather than
mirroring the GA4 interface (V1 spec s11).

Four reports per window:

    date                      -> ga4_daily            site totals
    date x pagePath           -> ga4_page_daily       per page
    date x <dimension>        -> ga4_dimension_daily  country, device, source...
    date x eventName          -> ga4_goal_daily       only the mapped goals

THE HONESTY CONSTRAINT: GA4 key events are named by whoever set the property
up, and nothing in `generate_lead` or `form_submit_2` says which one means "an
enquiry". The platform cannot infer it. Until the customer maps one, outcome
figures are ABSENT — the dashboard says "outcomes not configured" rather than
showing a conversion rate assembled from a guess.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from uuid import UUID

from psycopg import AsyncConnection

from api.hub.providers.google.client import GoogleClient
from api.hub.providers.google.errors import GoogleError, GoogleQuotaExceeded
from api.hub.repositories import HubRepository
from api.hub.services.sync.windows import DateWindow, month_chunks

logger = logging.getLogger("visibility_hub.sync.analytics")

SERVICE = "analytics"

#: GA4's default retention is 14 months. Asking for more returns nothing, so
#: the backfill stops there rather than burning quota on empty ranges.
BACKFILL_MONTHS = 14

#: GA4 keeps refining the last day or two. Same reasoning as Search Console:
#: showing a figure that will change is worse than showing it a day later.
REPORTING_LAG_DAYS = 2
RESTATEMENT_WINDOW_DAYS = 3

SITE_METRICS = [
    "sessions",
    "activeUsers",
    "engagedSessions",
    "engagementRate",
    "screenPageViews",
    "keyEvents",
]

#: The long tail from the spec's metric list, fetched into one table rather
#: than one table per dimension.
TAIL_DIMENSIONS = {
    "country": "country",
    "device_category": "deviceCategory",
    "session_source": "sessionSource",
    "session_medium": "sessionMedium",
    "session_default_channel_group": "sessionDefaultChannelGroup",
    "landing_page": "landingPagePlusQueryString",
}


def latest_available(today: date) -> date:
    return today - timedelta(days=REPORTING_LAG_DAYS)


def backfill_window(today: date, months: int = BACKFILL_MONTHS) -> DateWindow:
    end = latest_available(today)
    return DateWindow(end - timedelta(days=months * 30), end)


def incremental_window(today: date) -> DateWindow:
    end = latest_available(today)
    return DateWindow(end - timedelta(days=RESTATEMENT_WINDOW_DAYS - 1), end)


@dataclass(slots=True)
class SyncOutcome:
    rows_written: int = 0
    api_calls: int = 0
    quota_hits: int = 0
    chunks_completed: int = 0
    chunks_failed: int = 0
    goals_synced: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.chunks_failed and self.chunks_completed:
            return "partial"
        if self.chunks_failed:
            return "failed"
        return "succeeded"


class AnalyticsSync:
    def __init__(
        self,
        conn: AsyncConnection,
        client: GoogleClient,
        *,
        today: date | None = None,
    ) -> None:
        self._conn = conn
        self._client = client
        self._repo = HubRepository(conn)
        self._today = today or datetime.utcnow().date()

    async def backfill(self, **kwargs) -> SyncOutcome:
        window = backfill_window(self._today)
        await self._conn.execute(
            "select app.ensure_partitions_for_backfill(%s, %s)",
            (window.start, window.end),
        )
        return await self._run(window=window, kind="backfill", **kwargs)

    async def incremental(self, **kwargs) -> SyncOutcome:
        return await self._run(
            window=incremental_window(self._today), kind="incremental", **kwargs
        )

    async def _run(
        self,
        *,
        organization_id: UUID,
        website_id: UUID,
        link_id: UUID,
        property_uri: str,
        access_token: str,
        window: DateWindow,
        kind: str,
    ) -> SyncOutcome:
        run_id = await self._repo.start_sync_run(
            organization_id=organization_id,
            website_id=website_id,
            link_id=link_id,
            provider_key="google",
            service=SERVICE,
            kind=kind,
            range_start=window.start,
            range_end=window.end,
        )
        outcome = SyncOutcome()

        # Only events the customer has explicitly mapped. Syncing every key
        # event would fill the table with names nobody has given a meaning.
        goals = [g["event_name"] for g in await self._repo.goal_events(website_id)]

        for chunk in month_chunks(window):
            try:
                await self._sync_chunk(
                    organization_id=organization_id,
                    website_id=website_id,
                    property_uri=property_uri,
                    access_token=access_token,
                    chunk=chunk,
                    goals=goals,
                    outcome=outcome,
                )
                outcome.chunks_completed += 1
                await self._repo.record_synced_through(link_id, chunk.end)
            except GoogleQuotaExceeded:
                outcome.quota_hits += 1
                outcome.chunks_failed += 1
                outcome.failures.append(f"quota exceeded at {chunk.start}")
                break
            except GoogleError as exc:
                outcome.chunks_failed += 1
                outcome.failures.append(f"{chunk.start}: {exc.code}")

        await self._repo.finish_sync_run(
            run_id,
            status=outcome.status,
            rows_written=outcome.rows_written,
            api_calls=outcome.api_calls,
            quota_hits=outcome.quota_hits,
            error="; ".join(outcome.failures) or None,
        )
        if outcome.status == "succeeded" and kind == "backfill":
            await self._repo.record_backfill_complete(link_id)
        return outcome

    async def _sync_chunk(
        self,
        *,
        organization_id: UUID,
        website_id: UUID,
        property_uri: str,
        access_token: str,
        chunk: DateWindow,
        goals: list[str],
        outcome: SyncOutcome,
    ) -> None:
        start, end = chunk.as_api()

        async def report(dimensions: list[str], metrics: list[str]) -> list[list[str]]:
            outcome.api_calls += 1
            return await self._client.iter_report(
                access_token,
                property_uri,
                start_date=start,
                end_date=end,
                dimensions=dimensions,
                metrics=metrics,
            )

        totals = await report(["date"], SITE_METRICS)
        outcome.rows_written += await self._repo.upsert_ga4_daily(
            organization_id, website_id, [_site_row(r) for r in totals]
        )

        pages = await report(["date", "pagePath"], SITE_METRICS)
        outcome.rows_written += await self._repo.upsert_ga4_pages(
            organization_id, website_id, [_page_row(r) for r in pages]
        )

        for kind, ga4_name in TAIL_DIMENSIONS.items():
            rows = await report(["date", ga4_name], SITE_METRICS)
            outcome.rows_written += await self._repo.upsert_ga4_dimension(
                organization_id,
                website_id,
                kind,
                [_dimension_row(r) for r in rows],
            )

        if goals:
            events = await report(["date", "eventName"], ["eventCount"])
            mapped = [
                (_date(cells[0]), cells[1], int(float(cells[2])))
                for cells in events
                if cells[1] in goals
            ]
            outcome.goals_synced += len(mapped)
            outcome.rows_written += await self._repo.upsert_ga4_goals(
                organization_id, website_id, mapped
            )


def _date(value: str) -> date:
    # GA4 returns YYYYMMDD, not ISO.
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def _metrics(cells: list[str], offset: int) -> tuple:
    sessions, users, engaged, rate, views, key_events = cells[offset : offset + 6]
    return (
        int(float(sessions)),
        int(float(users)),
        int(float(engaged)),
        round(float(rate), 4),
        int(float(views)),
        int(float(key_events)),
    )


def _site_row(cells: list[str]) -> tuple:
    return (_date(cells[0]), *_metrics(cells, 1))


def _page_row(cells: list[str]) -> tuple:
    import hashlib

    path = cells[1]
    return (_date(cells[0]), hashlib.sha256(path.encode()).digest(), path,
            *_metrics(cells, 2))


def _dimension_row(cells: list[str]) -> tuple:
    return (_date(cells[0]), cells[1] or "(not set)", *_metrics(cells, 2))
