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

    # -- sync runs ---------------------------------------------------------

    async def start_sync_run(
        self,
        *,
        organization_id: UUID,
        website_id: UUID,
        link_id: UUID | None,
        provider_key: str,
        service: str,
        kind: str,
        range_start: Any,
        range_end: Any,
    ) -> UUID:
        row = await fetch_one(
            self._conn,
            """
            insert into sync_runs
                (organization_id, website_id, website_connection_id, provider_key,
                 service, kind, range_start, range_end, status)
            values (%s, %s, %s, %s, %s, %s, %s, %s, 'running')
            returning id
            """,
            (
                organization_id,
                website_id,
                link_id,
                provider_key,
                service,
                kind,
                range_start,
                range_end,
            ),
        )
        assert row is not None
        return row["id"]

    async def finish_sync_run(
        self,
        run_id: UUID,
        *,
        status: str,
        rows_written: int,
        api_calls: int,
        quota_hits: int,
        error: str | None,
    ) -> None:
        """A partial sync is recorded as `partial` with its range, so a gap in
        a chart is explainable and re-runnable rather than permanent."""
        await self._conn.execute(
            """
            update sync_runs
               set status = %s, rows_written = %s, api_calls = %s,
                   quota_hits = %s, error = %s, finished_at = now()
             where id = %s
            """,
            (status, rows_written, api_calls, quota_hits, error, run_id),
        )

    async def record_synced_through(self, link_id: UUID, through: Any) -> None:
        """Monotonic: an incremental run covering a trailing window must not
        drag `last_synced_date` backwards past what a backfill already got."""
        await self._conn.execute(
            "update website_connections "
            "   set last_synced_date = greatest(coalesce(last_synced_date, %s), %s) "
            " where id = %s",
            (through, through, link_id),
        )

    async def record_backfill_complete(self, link_id: UUID) -> None:
        await self._conn.execute(
            "update website_connections set backfill_completed_at = now() "
            " where id = %s and backfill_completed_at is null",
            (link_id,),
        )

    async def last_sync_runs(
        self, website_id: UUID, limit: int = 20
    ) -> list[dict[str, Any]]:
        return await fetch_all(
            self._conn,
            """
            select id, service, kind, status, range_start, range_end,
                   rows_written, api_calls, quota_hits, error,
                   started_at, finished_at
              from sync_runs
             where website_id = %s
             order by started_at desc
             limit %s
            """,
            (website_id, limit),
        )

    # -- Search Console facts ---------------------------------------------
    #
    # Upserts, not inserts: the nightly run re-fetches a trailing window
    # because Google restates recent days, so a second write for the same day
    # must replace the first rather than collide with it.

    async def upsert_gsc_totals(
        self, organization_id: UUID, website_id: UUID, rows: list[tuple]
    ) -> int:
        if not rows:
            return 0
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                insert into gsc_totals_daily
                    (organization_id, website_id, date, clicks, impressions, position)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (website_id, date) do update
                   set clicks = excluded.clicks,
                       impressions = excluded.impressions,
                       position = excluded.position
                """,
                [(organization_id, website_id, *r) for r in rows],
            )
        return len(rows)

    async def upsert_gsc_queries(
        self, organization_id: UUID, website_id: UUID, rows: list[tuple]
    ) -> int:
        if not rows:
            return 0
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                insert into gsc_query_daily
                    (organization_id, website_id, date, query_hash, query,
                     clicks, impressions, position)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (website_id, date, query_hash, country, device)
                do update set clicks = excluded.clicks,
                              impressions = excluded.impressions,
                              position = excluded.position
                """,
                [(organization_id, website_id, *r) for r in rows],
            )
        return len(rows)

    async def upsert_gsc_pages(
        self, organization_id: UUID, website_id: UUID, rows: list[tuple]
    ) -> int:
        if not rows:
            return 0
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                insert into gsc_page_daily
                    (organization_id, website_id, date, url_hash, url,
                     clicks, impressions, position)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (website_id, date, url_hash, country, device)
                do update set clicks = excluded.clicks,
                              impressions = excluded.impressions,
                              position = excluded.position
                """,
                [(organization_id, website_id, *r) for r in rows],
            )
        return len(rows)

    async def upsert_gsc_query_pages(
        self, organization_id: UUID, website_id: UUID, rows: list[tuple]
    ) -> int:
        if not rows:
            return 0
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                insert into gsc_query_page_daily
                    (organization_id, website_id, date, query_hash, query,
                     url_hash, url, clicks, impressions, position)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (website_id, date, query_hash, url_hash)
                do update set clicks = excluded.clicks,
                              impressions = excluded.impressions,
                              position = excluded.position
                """,
                [(organization_id, website_id, *r) for r in rows],
            )
        return len(rows)

    # -- GA4 facts ---------------------------------------------------------

    async def goal_events(self, website_id: UUID) -> list[dict[str, Any]]:
        """Which GA4 events this customer has said are their outcomes.

        Empty is a meaningful answer, not a missing one: the dashboard says
        "outcomes not configured" rather than inventing a conversion rate.
        """
        return await fetch_all(
            self._conn,
            "select event_name, label, goal_kind, is_primary from ga4_goal_events "
            " where website_id = %s order by is_primary desc, label",
            (website_id,),
        )

    async def set_goal_events(
        self, organization_id: UUID, website_id: UUID, goals: list[dict[str, Any]]
    ) -> int:
        """Replaces the mapping wholesale: the UI presents it as one choice,
        so a removed goal must actually disappear."""
        await self._conn.execute(
            "delete from ga4_goal_events where website_id = %s", (website_id,)
        )
        if not goals:
            return 0
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                insert into ga4_goal_events
                    (organization_id, website_id, event_name, label, goal_kind,
                     is_primary)
                values (%s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        organization_id,
                        website_id,
                        g["event_name"],
                        g.get("label") or g["event_name"],
                        g.get("goal_kind", "other"),
                        bool(g.get("is_primary")),
                    )
                    for g in goals
                ],
            )
        return len(goals)

    async def upsert_ga4_daily(
        self, organization_id: UUID, website_id: UUID, rows: list[tuple]
    ) -> int:
        if not rows:
            return 0
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                insert into ga4_daily
                    (organization_id, website_id, date, sessions, active_users,
                     engaged_sessions, engagement_rate, key_events)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (website_id, date, channel_group) do update
                   set sessions = excluded.sessions,
                       active_users = excluded.active_users,
                       engaged_sessions = excluded.engaged_sessions,
                       engagement_rate = excluded.engagement_rate,
                       key_events = excluded.key_events
                """,
                [
                    (organization_id, website_id, d, sess, users, engaged, rate, ke)
                    for (d, sess, users, engaged, rate, _views, ke) in rows
                ],
            )
        return len(rows)

    async def upsert_ga4_pages(
        self, organization_id: UUID, website_id: UUID, rows: list[tuple]
    ) -> int:
        if not rows:
            return 0
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                insert into ga4_page_daily
                    (organization_id, website_id, date, url_hash, page_path,
                     sessions, active_users, engaged_sessions, key_events)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (website_id, date, url_hash) do update
                   set sessions = excluded.sessions,
                       active_users = excluded.active_users,
                       engaged_sessions = excluded.engaged_sessions,
                       key_events = excluded.key_events
                """,
                [
                    (organization_id, website_id, d, h, path, sess, users, engaged, ke)
                    for (d, h, path, sess, users, engaged, _rate, _views, ke) in rows
                ],
            )
        return len(rows)

    async def upsert_ga4_dimension(
        self,
        organization_id: UUID,
        website_id: UUID,
        dimension_type: str,
        rows: list[tuple],
    ) -> int:
        if not rows:
            return 0
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                insert into ga4_dimension_daily
                    (organization_id, website_id, date, dimension_type,
                     dimension_value, sessions, active_users, engaged_sessions,
                     engagement_rate, page_views, key_events)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (website_id, date, dimension_type, dimension_value)
                do update set sessions = excluded.sessions,
                              active_users = excluded.active_users,
                              engaged_sessions = excluded.engaged_sessions,
                              engagement_rate = excluded.engagement_rate,
                              page_views = excluded.page_views,
                              key_events = excluded.key_events
                """,
                [
                    (
                        organization_id, website_id, d, dimension_type, value,
                        sess, users, engaged, rate, views, ke,
                    )
                    for (d, value, sess, users, engaged, rate, views, ke) in rows
                ],
            )
        return len(rows)

    async def upsert_ga4_goals(
        self, organization_id: UUID, website_id: UUID, rows: list[tuple]
    ) -> int:
        if not rows:
            return 0
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                insert into ga4_goal_daily
                    (organization_id, website_id, date, event_name, event_count)
                values (%s, %s, %s, %s, %s)
                on conflict (website_id, date, event_name, url_hash) do update
                   set event_count = excluded.event_count
                """,
                [
                    (organization_id, website_id, d, name, count)
                    for (d, name, count) in rows
                ],
            )
        return len(rows)

    async def active_link_for(
        self, website_id: UUID, service: str
    ) -> dict[str, Any] | None:
        return await fetch_one(
            self._conn,
            """
            select l.id as link_id, l.website_id, l.organization_id,
                   p.property_uri, p.connection_id, c.status as connection_status
              from website_connections l
              join connection_properties p on p.id = l.property_id
              join connections c on c.id = p.connection_id
             where l.website_id = %s and l.service = %s and l.status = 'active'
             limit 1
            """,
            (website_id, service),
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
