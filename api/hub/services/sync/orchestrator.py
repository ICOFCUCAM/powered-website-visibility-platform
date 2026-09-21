"""Synchronising one website's Google data, start to finish.

This is what "the Hub owns scheduled sync orchestration" means in practice
(docs/08-architecture.md). The nightly worker asks the Hub to sync a website;
it does not resolve a link, mint a token or build a client, because only the
Hub may do any of those.

Extracted from the two sync routes, which now call it as well. Before, the
choice between a 16-month backfill and a nightly incremental existed twice,
in two files, keyed off the same column — and a schedule that got that choice
wrong would spend a customer's entire Google quota re-fetching history every
night.

The choice is made from `backfill_completed_at` and never from a caller's
flag, for the same reason the route always did: a caller that could ask for a
backfill could ask for sixteen months of somebody's quota on every tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.hub import deps
from api.hub.providers.google.client import GoogleClient
from api.hub.repositories import HubRepository
from api.hub.services import events
from api.hub.services.keys import LocalKeyManager
from api.hub.services.sync.analytics import AnalyticsSync
from api.hub.services.sync.search_console import SearchConsoleSync
from api.hub.services.tokens import TokenService
from api.hub.services.vault import TokenVault

logger = logging.getLogger("visibility_hub.hub")

SERVICES = ("search_console", "analytics")


@dataclass(frozen=True, slots=True)
class Skipped:
    """Not an error. A website with no Analytics link has nothing to sync, and
    recording a failure every night would bury the real ones."""

    reason: str


async def sync_website(
    conn: AsyncConnection,
    *,
    website_id: UUID,
    service: str,
    client: GoogleClient | None = None,
    vault: TokenVault | None = None,
    today: date | None = None,
) -> Any | Skipped:
    """Returns the sync outcome, or `Skipped` with a reason.

    The connection must be the SERVICE role: this reads the token vault, and
    the tenant is taken from the link row rather than from an argument.

    `client` and `vault` are optional so a caller with nothing to inject — the
    nightly worker — never has to build either. That is what keeps the Hub
    boundary honest: the worker asks for a website to be synced and the Google
    client is constructed, used and discarded entirely inside the Hub, which
    an import contract can then check.
    """
    if service not in SERVICES:
        raise ValueError(f"unknown service {service!r}")

    if client is None or vault is None:
        settings = deps.google_settings_from_env()
        client = client or deps.google_client_from(settings)
        vault = vault or TokenVault(
            conn, LocalKeyManager.from_base64(settings.token_master_key)
        )

    repo = HubRepository(conn)
    link = await repo.active_link_for(website_id, service)
    if link is None:
        return Skipped(f"no active {service} link")
    if link["connection_status"] != "active":
        # A connection needing re-auth is the customer's to fix; hammering it
        # nightly turns one reconnect prompt into a wall of failures.
        return Skipped(f"connection is {link['connection_status']}")

    access_token = await TokenService(conn, vault, client).access_token_for(
        link["connection_id"]
    )

    links = await repo.active_links(website_id)
    backfilled = any(
        row["service"] == service and row["backfill_completed_at"] for row in links
    )
    kind = "incremental" if backfilled else "backfill"

    engine: Any = (
        SearchConsoleSync(conn, client, today=today)
        if service == "search_console"
        else AnalyticsSync(conn, client, today=today)
    )
    outcome = await (engine.incremental if backfilled else engine.backfill)(
        organization_id=link["organization_id"],
        website_id=website_id,
        link_id=link["link_id"],
        property_uri=link["property_uri"],
        access_token=access_token,
    )

    await events.publish(
        events.DomainEvent(
            events.SYNC_COMPLETED if outcome.status != "failed" else events.SYNC_FAILED,
            {
                "website_id": str(website_id),
                "service": service,
                "kind": kind,
                "rows_written": outcome.rows_written,
            },
        )
    )
    return outcome
