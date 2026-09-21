"""Crawl records."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one

_COLUMNS = """
    id, status, trigger, pages_discovered, pages_fetched, pages_rendered,
    fetch_errors, queued_at, started_at, finished_at, error_summary
"""


class CrawlRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def create(
        self, *, organization_id: UUID, website_id: UUID, trigger: str
    ) -> dict[str, Any]:
        row = await fetch_one(
            self._conn,
            f"""
            insert into crawls (organization_id, website_id, trigger, status)
            values (%s, %s, %s, 'queued')
            returning {_COLUMNS}
            """,
            (organization_id, website_id, trigger),
        )
        assert row is not None
        return row

    async def running_for(self, website_id: UUID) -> bool:
        row = await fetch_one(
            self._conn,
            "select exists(select 1 from crawls where website_id = %s "
            "  and status in ('queued','running')) as running",
            (website_id,),
        )
        return bool(row and row["running"])

    async def list_for(self, website_id: UUID, limit: int = 20) -> list[dict[str, Any]]:
        return await fetch_all(
            self._conn,
            f"select {_COLUMNS} from crawls where website_id = %s "
            " order by queued_at desc limit %s",
            (website_id, limit),
        )

    async def get(self, website_id: UUID, crawl_id: UUID) -> dict[str, Any] | None:
        return await fetch_one(
            self._conn,
            f"select {_COLUMNS} from crawls where website_id = %s and id = %s",
            (website_id, crawl_id),
        )
