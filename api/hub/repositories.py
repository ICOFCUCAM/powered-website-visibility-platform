"""Persistence for the Hub's own tables.

The Hub owns `connections`, `connection_properties`, `website_connections` and
`sync_runs`, and reads nothing else. It never touches crawl, issue or scoring
tables — that boundary is what lets it be lifted out later.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one


class HubRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    # -- connections -------------------------------------------------------

    async def upsert_connection(
        self,
        *,
        organization_id: UUID,
        provider_key: str,
        external_id: str,
        label: str,
        granted_scopes: list[str],
        refresh_token_id: UUID | None,
        connected_by: UUID | None,
        access_token_expires_at: Any = None,
    ) -> dict[str, Any]:
        """Keyed on (organization, provider, external_id).

        `external_id` is Google's `sub`, never the email: a user who changes
        their Google email must not silently fork into a second connection.

        On reconnect the refresh token is only replaced when Google issued a
        new one — a re-consent without `prompt=consent` returns no refresh
        token, and overwriting the stored one with NULL would break sync.
        """
        row = await fetch_one(
            self._conn,
            """
            insert into connections
                (organization_id, provider_key, external_id, label,
                 granted_scopes, refresh_token_id, connected_by,
                 access_token_expires_at, status)
            values (%s, %s, %s, %s, %s, %s, %s, %s, 'active')
            on conflict (organization_id, provider_key, external_id) do update
               set label = excluded.label,
                   granted_scopes = excluded.granted_scopes,
                   refresh_token_id =
                       coalesce(excluded.refresh_token_id,
                                connections.refresh_token_id),
                   access_token_expires_at = excluded.access_token_expires_at,
                   status = 'active',
                   last_error = null,
                   revoked_at = null,
                   last_refreshed_at = now()
            returning id, organization_id, provider_key, external_id,
                      label::text as label, granted_scopes, refresh_token_id,
                      status, created_at
            """,
            (
                organization_id,
                provider_key,
                external_id,
                label,
                granted_scopes,
                refresh_token_id,
                connected_by,
                access_token_expires_at,
            ),
        )
        assert row is not None
        return row

    async def list_connections(self, organization_id: UUID) -> list[dict[str, Any]]:
        return await fetch_all(
            self._conn,
            """
            select id, provider_key, label::text as label, granted_scopes,
                   status, last_error, created_at, last_refreshed_at
              from connections
             where organization_id = %s and revoked_at is null
             order by created_at
            """,
            (organization_id,),
        )

    async def get_connection(self, connection_id: UUID) -> dict[str, Any] | None:
        return await fetch_one(
            self._conn,
            """
            select id, organization_id, provider_key, external_id,
                   label::text as label, granted_scopes, refresh_token_id,
                   status
              from connections where id = %s
            """,
            (connection_id,),
        )

    async def mark_needs_reauth(self, connection_id: UUID, reason: str) -> None:
        """Stop scheduling work against a dead grant rather than retrying it."""
        await self._conn.execute(
            "update connections set status = 'needs_reauth', last_error = %s "
            "where id = %s",
            (reason, connection_id),
        )

    async def mark_revoked(self, connection_id: UUID) -> None:
        await self._conn.execute(
            "update connections "
            "   set status = 'revoked', revoked_at = now(), refresh_token_id = null "
            " where id = %s",
            (connection_id,),
        )

    # -- properties --------------------------------------------------------

    async def upsert_property(
        self,
        *,
        organization_id: UUID,
        connection_id: UUID,
        provider_key: str,
        service: str,
        property_uri: str,
        property_name: str | None,
        permission_level: str | None,
        matched_hosts: list[str],
        raw: str,
    ) -> dict[str, Any]:
        row = await fetch_one(
            self._conn,
            """
            insert into connection_properties
                (organization_id, connection_id, provider_key, service,
                 property_uri, property_name, permission_level, matched_hosts, raw)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (connection_id, service, property_uri) do update
               set property_name = excluded.property_name,
                   permission_level = excluded.permission_level,
                   matched_hosts = excluded.matched_hosts,
                   raw = excluded.raw,
                   last_seen_at = now()
            returning id, service, property_uri, property_name,
                      permission_level, matched_hosts
            """,
            (
                organization_id,
                connection_id,
                provider_key,
                service,
                property_uri,
                property_name,
                permission_level,
                matched_hosts,
                raw,
            ),
        )
        assert row is not None
        return row

    async def list_properties(
        self, organization_id: UUID, service: str
    ) -> list[dict[str, Any]]:
        return await fetch_all(
            self._conn,
            """
            select p.id, p.service, p.property_uri, p.property_name,
                   p.permission_level, p.matched_hosts, p.connection_id,
                   c.label::text as account_label
              from connection_properties p
              join connections c on c.id = p.connection_id
             where p.organization_id = %s and p.service = %s
               and c.revoked_at is null
             order by p.property_uri
            """,
            (organization_id, service),
        )

    async def get_property(self, property_id: UUID) -> dict[str, Any] | None:
        return await fetch_one(
            self._conn,
            """
            select id, organization_id, connection_id, provider_key, service,
                   property_uri, property_name, permission_level, matched_hosts
              from connection_properties where id = %s
            """,
            (property_id,),
        )

    # -- links -------------------------------------------------------------

    async def link_property(
        self,
        *,
        organization_id: UUID,
        website_id: UUID,
        property_id: UUID,
        provider_key: str,
        service: str,
        link_method: str,
    ) -> dict[str, Any]:
        """One active link per (website, service).

        The existing link is unlinked rather than deleted, so the history of
        what fed a website's numbers survives a reconnection — otherwise a
        changed property silently rewrites the past.
        """
        await self._conn.execute(
            "update website_connections set status = 'unlinked' "
            " where website_id = %s and service = %s and status = 'active'",
            (website_id, service),
        )
        row = await fetch_one(
            self._conn,
            """
            insert into website_connections
                (organization_id, website_id, property_id, provider_key,
                 service, link_method, status)
            values (%s, %s, %s, %s, %s, %s, 'active')
            returning id, website_id, property_id, service, link_method, status
            """,
            (
                organization_id,
                website_id,
                property_id,
                provider_key,
                service,
                link_method,
            ),
        )
        assert row is not None
        return row

    async def active_links(self, website_id: UUID) -> list[dict[str, Any]]:
        return await fetch_all(
            self._conn,
            """
            select l.id, l.service, l.link_method, l.status,
                   l.backfill_completed_at, l.last_synced_date,
                   p.property_uri, p.property_name, p.permission_level,
                   c.label::text as account_label, c.status as connection_status
              from website_connections l
              join connection_properties p on p.id = l.property_id
              join connections c on c.id = p.connection_id
             where l.website_id = %s and l.status = 'active'
             order by l.service
            """,
            (website_id,),
        )

    async def ownership_evidence(self, website_id: UUID) -> dict[str, Any] | None:
        """Does a linked property actually cover the canonical URL?

        Delegates to migration 0010 rather than reimplementing coverage here,
        so there is exactly one definition of it.
        """
        return await fetch_one(
            self._conn,
            """
            select property_id, property_uri, permission_level,
                   covers_canonical_url, is_sufficient_evidence
              from website_ownership_evidence
             where website_id = %s and is_sufficient_evidence
             limit 1
            """,
            (website_id,),
        )

    async def record_ownership(
        self, website_id: UUID, property_id: UUID, method: str
    ) -> None:
        """Ownership is written by the service role only: migration 0012
        revokes these columns from client roles."""
        await self._conn.execute(
            """
            update websites
               set ownership_verified_at = now(),
                   ownership_method = %s,
                   ownership_property_id = %s,
                   status = case when status = 'PENDING' then 'CONNECTING'
                                 else status end
             where id = %s
            """,
            (method, property_id, website_id),
        )
