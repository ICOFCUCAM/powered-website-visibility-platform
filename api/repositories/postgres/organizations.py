"""Postgres implementation of the organisation repository."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one
from api.domain.models import Membership, Organization, Plan, Role


class PostgresOrganizationRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def get(self, organization_id: UUID) -> Organization | None:
        row = await fetch_one(
            self._conn,
            """
            select id, name, slug::text as slug, plan,
                   max_websites, max_pages_per_crawl
              from organizations
             where id = %s
            """,
            (organization_id,),
        )
        if row is None:
            return None
        return Organization(
            id=row["id"],
            name=row["name"],
            slug=row["slug"],
            plan=Plan(row["plan"]),
            max_websites=row["max_websites"],
            max_pages_per_crawl=row["max_pages_per_crawl"],
        )

    async def memberships_for_user(self, user_id: UUID) -> list[Membership]:
        rows = await fetch_all(
            self._conn,
            """
            select organization_id, user_id, role
              from organization_members
             where user_id = %s
             order by created_at
            """,
            (user_id,),
        )
        return [
            Membership(
                organization_id=r["organization_id"],
                user_id=r["user_id"],
                role=Role(r["role"]),
            )
            for r in rows
        ]

    async def count_websites(self, organization_id: UUID) -> int:
        row = await fetch_one(
            self._conn,
            "select count(*) as n from websites "
            "where organization_id = %s and archived_at is null",
            (organization_id,),
        )
        return int(row["n"]) if row else 0
