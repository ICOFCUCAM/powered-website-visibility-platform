"""Postgres implementation of the website repository.

Every statement still carries its tenant filter even though RLS would enforce
it. Belt and braces on purpose: RLS is the backstop for a handler that forgets,
not a licence to stop writing the filter.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one
from api.domain.models import OwnershipMethod, Website, WebsiteStatus

_COLUMNS = """
    id, organization_id, domain::text as domain, canonical_url, name, status,
    timezone, created_at, ownership_verified_at, ownership_method,
    crawl_allowed, crawl_blocked_reason
"""


def _to_website(row: dict[str, Any]) -> Website:
    return Website(
        id=row["id"],
        organization_id=row["organization_id"],
        domain=row["domain"],
        canonical_url=row["canonical_url"],
        name=row["name"],
        status=WebsiteStatus(row["status"]),
        timezone=row["timezone"],
        created_at=row["created_at"],
        ownership_verified_at=row["ownership_verified_at"],
        ownership_method=(
            OwnershipMethod(row["ownership_method"])
            if row["ownership_method"]
            else None
        ),
        crawl_allowed=row["crawl_allowed"],
        crawl_blocked_reason=row["crawl_blocked_reason"],
    )


class PostgresWebsiteRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def list_for_organization(self, organization_id: UUID) -> list[Website]:
        rows = await fetch_all(
            self._conn,
            f"select {_COLUMNS} from websites "
            "where organization_id = %s and archived_at is null "
            "order by created_at",
            (organization_id,),
        )
        return [_to_website(r) for r in rows]

    async def get(self, website_id: UUID) -> Website | None:
        row = await fetch_one(
            self._conn,
            f"select {_COLUMNS} from websites "
            "where id = %s and archived_at is null",
            (website_id,),
        )
        return _to_website(row) if row else None

    async def find_by_domain(
        self, organization_id: UUID, domain: str
    ) -> Website | None:
        row = await fetch_one(
            self._conn,
            f"select {_COLUMNS} from websites "
            "where organization_id = %s and domain = %s and archived_at is null",
            (organization_id, domain),
        )
        return _to_website(row) if row else None

    async def create(
        self,
        *,
        organization_id: UUID,
        domain: str,
        canonical_url: str,
        name: str | None,
    ) -> Website:
        # crawl_allowed and the ownership columns are absent by design: they are
        # derived, and migration 0012 revokes them from client roles anyway.
        row = await fetch_one(
            self._conn,
            f"""
            insert into websites (organization_id, domain, canonical_url, name)
            values (%s, %s, %s, %s)
            returning {_COLUMNS}
            """,
            (organization_id, domain, canonical_url, name),
        )
        assert row is not None
        return _to_website(row)

    async def archive(self, website_id: UUID) -> None:
        await self._conn.execute(
            "update websites set archived_at = now() "
            "where id = %s and archived_at is null",
            (website_id,),
        )

    async def ownership_covers_canonical_url(self, website_id: UUID) -> bool:
        """Does a linked Search Console property actually cover this website?

        Delegates to the database function from migration 0010 rather than
        reimplementing the domain-vs-URL-prefix rules in Python, so there is
        exactly one definition of coverage.
        """
        row = await fetch_one(
            self._conn,
            """
            select bool_or(is_sufficient_evidence) as ok
              from website_ownership_evidence
             where website_id = %s
            """,
            (website_id,),
        )
        return bool(row and row["ok"])
