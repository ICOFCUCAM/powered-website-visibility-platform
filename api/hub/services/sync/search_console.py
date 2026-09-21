"""Search Console synchronisation.

THE INVARIANT THIS SERVICE EXISTS TO PROTECT (docs/01-data-model.md):

    Dimensional datasets are never summed across incompatible dimensions to
    produce site totals. `gsc_totals_daily` is fetched separately as the
    reconciliation authority; the dimensional tables are analytical subsets
    whose missing-query share is explicitly represented.

That is not fastidiousness. Google withholds low-volume queries from any
response carrying a `query` dimension — commonly 30-50% of clicks — so a total
derived by summing query rows contradicts what the customer sees in their own
Search Console, and they will believe Google.

Four fetches per window, each a separate API request with its own dimensions:

    ["date"]                  -> gsc_totals_daily        reconciliation authority
    ["date", "query"]         -> gsc_query_daily         analytical
    ["date", "page"]          -> gsc_page_daily          analytical
    ["date", "query", "page"] -> gsc_query_page_daily    analytical, 90 days
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.hub.providers.google.client import GoogleClient
from api.hub.providers.google.errors import GoogleError, GoogleQuotaExceeded
from api.hub.repositories import HubRepository
from api.hub.services.sync.windows import (
    QUERY_PAGE_WINDOW_DAYS,
    DateWindow,
    backfill_window,
    incremental_window,
    month_chunks,
)

logger = logging.getLogger("visibility_hub.sync.search_console")

SERVICE = "search_console"

#: Cap on query x page rows kept per chunk. This dataset grows fastest and is
#: read least; the tail is noise for the findings that use it.
QUERY_PAGE_MAX_ROWS = 50_000


def sha256_bytes(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


@dataclass(slots=True)
class SyncOutcome:
    rows_written: int = 0
    api_calls: int = 0
    quota_hits: int = 0
    chunks_completed: int = 0
    chunks_failed: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.chunks_failed and self.chunks_completed:
            return "partial"
        if self.chunks_failed:
            return "failed"
        return "succeeded"


class SearchConsoleSync:
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

    # -- entry points ------------------------------------------------------

    async def backfill(
        self,
        *,
        organization_id: UUID,
        website_id: UUID,
        link_id: UUID,
        property_uri: str,
        access_token: str,
    ) -> SyncOutcome:
        """The 16-month grab, run once on connect.

        This is the product's activation moment: a brand-new account sees a
        populated trend chart within minutes, before the crawler has fetched a
        single page. It is also the only chance to capture history Google
        deletes at 16 months.
        """
        window = backfill_window(self._today)

        # A missing partition is an INSERT failure, not a silent drop, and the
        # scheduler only ever looks forwards. Create what this window needs.
        await self._conn.execute(
            "select app.ensure_partitions_for_backfill(%s, %s)",
            (window.start, window.end),
        )

        return await self._run(
            organization_id=organization_id,
            website_id=website_id,
            link_id=link_id,
            property_uri=property_uri,
            access_token=access_token,
            window=window,
            kind="backfill",
        )

    async def incremental(
        self,
        *,
        organization_id: UUID,
        website_id: UUID,
        link_id: UUID,
        property_uri: str,
        access_token: str,
    ) -> SyncOutcome:
        """The nightly re-fetch of a trailing window, upserted over what is
        already stored, because Google restates recent days."""
        return await self._run(
            organization_id=organization_id,
            website_id=website_id,
            link_id=link_id,
            property_uri=property_uri,
            access_token=access_token,
            window=incremental_window(self._today),
            kind="incremental",
        )

    # -- the loop ----------------------------------------------------------

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
        query_page_cutoff = _cutoff(self._today, QUERY_PAGE_WINDOW_DAYS)

        # Oldest first, so an interrupted backfill leaves a contiguous history
        # rather than a hole in the middle of the chart.
        for chunk in month_chunks(window):
            try:
                await self._sync_chunk(
                    organization_id=organization_id,
                    website_id=website_id,
                    property_uri=property_uri,
                    access_token=access_token,
                    chunk=chunk,
                    outcome=outcome,
                    include_query_page=chunk.end >= query_page_cutoff,
                )
                outcome.chunks_completed += 1
                await self._repo.record_synced_through(link_id, chunk.end)
            except GoogleQuotaExceeded:
                # Quota is a reason to stop, not to fail: what landed is real,
                # and the next run resumes from last_synced_date.
                outcome.quota_hits += 1
                outcome.chunks_failed += 1
                outcome.failures.append(f"quota exceeded at {chunk.start}")
                logger.warning("quota exceeded during %s at %s", kind, chunk.start)
                break
            except GoogleError as exc:
                outcome.chunks_failed += 1
                outcome.failures.append(f"{chunk.start}: {exc.code}")
                logger.warning("chunk failed during %s at %s", kind, chunk.start)

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
        outcome: SyncOutcome,
        include_query_page: bool,
    ) -> None:
        start, end = chunk.as_api()

        async def fetch(dimensions: list[str], max_rows: int | None = None):
            outcome.api_calls += 1
            return await self._client.iter_search_analytics(
                access_token,
                property_uri,
                start_date=start,
                end_date=end,
                dimensions=dimensions,
                max_rows=max_rows,
            )

        # 1. THE RECONCILIATION AUTHORITY. No `query` dimension, so Google
        #    withholds nothing and these totals match the Search Console UI.
        totals = await fetch(["date"])
        outcome.rows_written += await self._repo.upsert_gsc_totals(
            organization_id, website_id, _totals_rows(totals)
        )

        # 2-4. Analytical subsets. Never summed to produce a site total.
        queries = await fetch(["date", "query"])
        outcome.rows_written += await self._repo.upsert_gsc_queries(
            organization_id, website_id, _query_rows(queries)
        )

        pages = await fetch(["date", "page"])
        outcome.rows_written += await self._repo.upsert_gsc_pages(
            organization_id, website_id, _page_rows(pages)
        )

        if include_query_page:
            pairs = await fetch(["date", "query", "page"], QUERY_PAGE_MAX_ROWS)
            outcome.rows_written += await self._repo.upsert_gsc_query_pages(
                organization_id, website_id, _query_page_rows(pairs)
            )


def _cutoff(today: date, days: int) -> date:
    from datetime import timedelta

    return today - timedelta(days=days)


def _d(value: str) -> date:
    return date.fromisoformat(value)


def _totals_rows(rows: list[dict[str, Any]]) -> list[tuple]:
    return [
        (
            _d(r["keys"][0]),
            int(r.get("clicks", 0)),
            int(r.get("impressions", 0)),
            float(r.get("position", 0.0)),
        )
        for r in rows
    ]


def _query_rows(rows: list[dict[str, Any]]) -> list[tuple]:
    out = []
    for r in rows:
        day, query = r["keys"][0], r["keys"][1]
        out.append(
            (
                _d(day),
                sha256_bytes(query.lower()),
                query,
                int(r.get("clicks", 0)),
                int(r.get("impressions", 0)),
                float(r.get("position", 0.0)),
            )
        )
    return out


def _page_rows(rows: list[dict[str, Any]]) -> list[tuple]:
    out = []
    for r in rows:
        day, url = r["keys"][0], r["keys"][1]
        out.append(
            (
                _d(day),
                sha256_bytes(url),
                url,
                int(r.get("clicks", 0)),
                int(r.get("impressions", 0)),
                float(r.get("position", 0.0)),
            )
        )
    return out


def _query_page_rows(rows: list[dict[str, Any]]) -> list[tuple]:
    out = []
    for r in rows:
        day, query, url = r["keys"][0], r["keys"][1], r["keys"][2]
        out.append(
            (
                _d(day),
                sha256_bytes(query.lower()),
                query,
                sha256_bytes(url),
                url,
                int(r.get("clicks", 0)),
                int(r.get("impressions", 0)),
                float(r.get("position", 0.0)),
            )
        )
    return out
