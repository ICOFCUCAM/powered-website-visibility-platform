"""GA4 goal mapping and sync (V1 spec s10-11).

The goal-mapping step exists because GA4 key events are named by whoever set
the property up. `generate_lead`, `form_submit_2`, `donate` — nothing in the
name says which one means "an enquiry" for this business. The platform asks
rather than guesses, and until it is answered outcome figures are absent.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Path
from pydantic import BaseModel, Field

from api.deps import MembershipsDep, WebsiteRepoDep
from api.domain.errors import NotFound
from api.hub.deps import GoogleClientDep, ServiceConnectionDep, TokenVaultDep
from api.hub.providers.google.errors import GoogleRefreshRejected
from api.hub.repositories import HubRepository
from api.hub.services.sync.orchestrator import Skipped, sync_website
from api.hub.services.tokens import TokenService

router = APIRouter(prefix="/websites", tags=["analytics"])

GoalKind = Literal["contact", "purchase", "donation", "signup", "other"]


class KeyEventOut(BaseModel):
    event_name: str
    # GA4 marks some events as defaults it created. Surfacing that helps the
    # customer tell their own conversion apart from a built-in.
    custom: bool
    counting: str | None


class GoalIn(BaseModel):
    event_name: str = Field(min_length=1, max_length=120)
    label: str | None = Field(default=None, max_length=120)
    goal_kind: GoalKind = "other"
    is_primary: bool = False


class GoalsIn(BaseModel):
    goals: list[GoalIn] = Field(default_factory=list, max_length=10)


class GoalOut(BaseModel):
    event_name: str
    label: str
    goal_kind: str
    is_primary: bool


class AnalyticsSyncOut(BaseModel):
    status: str
    rows_written: int
    api_calls: int
    quota_hits: int
    goals_synced: int


async def _link(website_id, memberships, websites, repo):
    website = await websites.get(website_id)
    owned = {m.organization_id for m in memberships}
    if website is None or website.organization_id not in owned:
        raise NotFound("We couldn't find that website.")
    link = await repo.active_link_for(website_id, "analytics")
    if link is None:
        raise NotFound("That website isn't connected to Google Analytics yet.")
    return website, link


@router.get("/{website_id}/analytics/events", response_model=list[KeyEventOut])
async def list_key_events(
    website_id: Annotated[UUID, Path()],
    memberships: MembershipsDep,
    websites: WebsiteRepoDep,
    conn: ServiceConnectionDep,
    vault: TokenVaultDep,
    client: GoogleClientDep,
) -> list[KeyEventOut]:
    repo = HubRepository(conn)
    _, link = await _link(website_id, memberships, websites, repo)

    token = await TokenService(conn, vault, client).access_token_for(
        link["connection_id"]
    )
    raw = await client.list_key_events(token, link["property_uri"])
    return [
        KeyEventOut(
            event_name=e.get("eventName", ""),
            custom=bool(e.get("custom")),
            counting=e.get("countingMethod"),
        )
        for e in raw
        if e.get("eventName")
    ]


@router.get("/{website_id}/analytics/goals", response_model=list[GoalOut])
async def list_goals(
    website_id: Annotated[UUID, Path()],
    memberships: MembershipsDep,
    websites: WebsiteRepoDep,
    conn: ServiceConnectionDep,
) -> list[GoalOut]:
    website = await websites.get(website_id)
    owned = {m.organization_id for m in memberships}
    if website is None or website.organization_id not in owned:
        raise NotFound("We couldn't find that website.")
    return [
        GoalOut(**row) for row in await HubRepository(conn).goal_events(website_id)
    ]


@router.post("/{website_id}/analytics/goals", response_model=list[GoalOut])
async def set_goals(
    website_id: Annotated[UUID, Path()],
    body: GoalsIn,
    memberships: MembershipsDep,
    websites: WebsiteRepoDep,
    conn: ServiceConnectionDep,
) -> list[GoalOut]:
    website = await websites.get(website_id)
    owned = {m.organization_id for m in memberships}
    if website is None or website.organization_id not in owned:
        raise NotFound("We couldn't find that website.")

    repo = HubRepository(conn)
    await repo.set_goal_events(
        website.organization_id,
        website_id,
        [g.model_dump() for g in body.goals],
    )
    return [GoalOut(**row) for row in await repo.goal_events(website_id)]


@router.post("/{website_id}/sync/analytics", response_model=AnalyticsSyncOut)
async def sync_analytics(
    website_id: Annotated[UUID, Path()],
    memberships: MembershipsDep,
    websites: WebsiteRepoDep,
    conn: ServiceConnectionDep,
    vault: TokenVaultDep,
    client: GoogleClientDep,
) -> AnalyticsSyncOut:
    repo = HubRepository(conn)
    await _link(website_id, memberships, websites, repo)

    outcome = await sync_website(
        conn, website_id=website_id, service="analytics",
        client=client, vault=vault,
    )
    if isinstance(outcome, Skipped):
        raise GoogleRefreshRejected()

    return AnalyticsSyncOut(
        status=outcome.status,
        rows_written=outcome.rows_written,
        api_calls=outcome.api_calls,
        quota_hits=outcome.quota_hits,
        goals_synced=outcome.goals_synced,
    )
