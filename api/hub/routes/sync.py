"""Triggering and inspecting synchronisation."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path
from pydantic import BaseModel

from api.deps import MembershipsDep, WebsiteRepoDep
from api.domain.errors import NotFound
from api.hub.deps import GoogleClientDep, ServiceConnectionDep, TokenVaultDep
from api.hub.providers.google.errors import GoogleRefreshRejected
from api.hub.repositories import HubRepository
from api.hub.services import events
from api.hub.services.sync.search_console import SearchConsoleSync
from api.hub.services.tokens import TokenService

router = APIRouter(prefix="/websites", tags=["google"])


class SyncRunOut(BaseModel):
    id: str
    service: str
    kind: str
    status: str
    range_start: date | None
    range_end: date | None
    rows_written: int
    api_calls: int
    quota_hits: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class SyncResultOut(BaseModel):
    status: str
    rows_written: int
    api_calls: int
    quota_hits: int
    chunks_completed: int
    chunks_failed: int


async def _authorised_link(website_id, memberships, websites, repo, service):
    website = await websites.get(website_id)
    owned = {m.organization_id for m in memberships}
    if website is None or website.organization_id not in owned:
        raise NotFound("We couldn't find that website.")
    link = await repo.active_link_for(website_id, service)
    if link is None:
        raise NotFound("That website isn't connected to Search Console yet.")
    return website, link


@router.post("/{website_id}/sync/search-console", response_model=SyncResultOut)
async def sync_search_console(
    website_id: Annotated[UUID, Path()],
    memberships: MembershipsDep,
    websites: WebsiteRepoDep,
    conn: ServiceConnectionDep,
    vault: TokenVaultDep,
    client: GoogleClientDep,
) -> SyncResultOut:
    """Backfill on first run, incremental thereafter.

    Choosing by `backfill_completed_at` rather than by a caller-supplied flag:
    a client that could ask for a backfill could ask for sixteen months of
    someone's quota on every page load.
    """
    repo = HubRepository(conn)
    _, link = await _authorised_link(
        website_id, memberships, websites, repo, "search_console"
    )

    if link["connection_status"] != "active":
        raise GoogleRefreshRejected()

    tokens = TokenService(conn, vault, client)
    access_token = await tokens.access_token_for(link["connection_id"])
    sync = SearchConsoleSync(conn, client)

    already = await repo.active_links(website_id)
    backfilled = any(
        row["service"] == "search_console" and row["backfill_completed_at"]
        for row in already
    )

    run = sync.incremental if backfilled else sync.backfill
    outcome = await run(
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
                "service": "search_console",
                "kind": "incremental" if backfilled else "backfill",
                "rows_written": outcome.rows_written,
            },
        )
    )

    return SyncResultOut(
        status=outcome.status,
        rows_written=outcome.rows_written,
        api_calls=outcome.api_calls,
        quota_hits=outcome.quota_hits,
        chunks_completed=outcome.chunks_completed,
        chunks_failed=outcome.chunks_failed,
    )


@router.get("/{website_id}/sync-runs", response_model=list[SyncRunOut])
async def list_sync_runs(
    website_id: Annotated[UUID, Path()],
    memberships: MembershipsDep,
    websites: WebsiteRepoDep,
    conn: ServiceConnectionDep,
) -> list[SyncRunOut]:
    """Visible sync history. A user who can see that Tuesday's sync failed will
    not open a ticket about a dip in a chart."""
    website = await websites.get(website_id)
    owned = {m.organization_id for m in memberships}
    if website is None or website.organization_id not in owned:
        raise NotFound("We couldn't find that website.")

    repo = HubRepository(conn)
    return [
        SyncRunOut(
            id=str(r["id"]),
            service=r["service"],
            kind=r["kind"],
            status=r["status"],
            range_start=r["range_start"],
            range_end=r["range_end"],
            rows_written=r["rows_written"],
            api_calls=r["api_calls"],
            quota_hits=r["quota_hits"],
            error=r["error"],
            started_at=r["started_at"],
            finished_at=r["finished_at"],
        )
        for r in await repo.last_sync_runs(website_id)
    ]
