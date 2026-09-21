"""Property discovery and linking (V1 spec s30, s8).

"Do not assume the user's first Google property is the correct one" — so every
list is returned with an explicit match quality and the auto-match is a
*suggestion* the user can override. `link_method` records which happened, so a
wrong auto-match is diagnosable months later.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel

from api.deps import MembershipsDep, WebsiteRepoDep
from api.domain.errors import NotFound
from api.hub.deps import ServiceConnectionDep
from api.hub.repositories import HubRepository
from api.hub.services import events
from api.hub.services.matching import (
    MatchQuality,
    score_analytics_property,
    score_search_console_property,
)

router = APIRouter(prefix="/google", tags=["google"])


class PropertyOut(BaseModel):
    id: str
    service: str
    property_uri: str
    property_name: str | None
    permission_level: str | None
    account: str
    match_quality: str
    suggested: bool
    proves_ownership: bool
    # Numeric rank, so the ordering the matcher establishes survives the trip
    # to the client. Sorting by name would put a URL-prefix property above the
    # domain property that should be pre-ticked.
    match_rank: int


class LinkOut(BaseModel):
    id: str
    website_id: str
    service: str
    property_uri: str
    link_method: str
    ownership_recorded: bool


_QUALITY_NAMES = {
    MatchQuality.NONE: "none",
    MatchQuality.SUBDOMAIN: "subdomain",
    MatchQuality.URL_PREFIX_HOST: "host",
    MatchQuality.DOMAIN_PROPERTY: "domain",
}


async def _properties_for(
    service: str,
    website_id: UUID | None,
    memberships,
    websites,
    conn,
) -> list[PropertyOut]:
    repo = HubRepository(conn)
    website = await websites.get(website_id) if website_id else None
    owned = {m.organization_id for m in memberships}
    if website is not None and website.organization_id not in owned:
        raise NotFound("We couldn't find that website.")

    out: list[PropertyOut] = []
    for membership in memberships:
        for row in await repo.list_properties(membership.organization_id, service):
            if website is None:
                match = None
            elif service == "search_console":
                match = score_search_console_property(
                    website.domain, row["property_uri"], row["permission_level"]
                )
            else:
                match = score_analytics_property(
                    website.domain,
                    row["property_uri"],
                    [f"https://{h}" for h in (row["matched_hosts"] or [])],
                    row["property_name"],
                )

            quality = match.quality if match else MatchQuality.NONE
            out.append(
                PropertyOut(
                    id=str(row["id"]),
                    service=row["service"],
                    property_uri=row["property_uri"],
                    property_name=row["property_name"],
                    permission_level=row["permission_level"],
                    account=row["account_label"],
                    match_quality=_QUALITY_NAMES[quality],
                    suggested=bool(quality >= MatchQuality.URL_PREFIX_HOST),
                    proves_ownership=bool(match and match.proves_ownership),
                    match_rank=int(quality),
                )
            )

    # Best match first, so the pre-ticked option is at the top of the list. A
    # domain property outranks a URL-prefix one even though both match.
    return sorted(out, key=lambda p: (-p.match_rank, p.property_uri))


@router.get("/search-console/properties", response_model=list[PropertyOut])
async def search_console_properties(
    memberships: MembershipsDep,
    websites: WebsiteRepoDep,
    conn: ServiceConnectionDep,
    website_id: Annotated[UUID | None, Query()] = None,
) -> list[PropertyOut]:
    return await _properties_for(
        "search_console", website_id, memberships, websites, conn
    )


@router.get("/analytics/properties", response_model=list[PropertyOut])
async def analytics_properties(
    memberships: MembershipsDep,
    websites: WebsiteRepoDep,
    conn: ServiceConnectionDep,
    website_id: Annotated[UUID | None, Query()] = None,
) -> list[PropertyOut]:
    return await _properties_for("analytics", website_id, memberships, websites, conn)


async def _connect(
    service: str,
    property_id: UUID,
    website_id: UUID,
    memberships,
    websites,
    conn,
    link_method: str,
) -> LinkOut:
    repo = HubRepository(conn)
    owned = {m.organization_id for m in memberships}

    prop = await repo.get_property(property_id)
    website = await websites.get(website_id)
    if (
        prop is None
        or website is None
        or prop["organization_id"] not in owned
        or website.organization_id not in owned
        or prop["service"] != service
    ):
        raise NotFound("We couldn't find that property.")

    link = await repo.link_property(
        organization_id=website.organization_id,
        website_id=website_id,
        property_id=property_id,
        provider_key=prop["provider_key"],
        service=service,
        link_method=link_method,
    )

    # Ownership is recorded only when the linked property genuinely covers the
    # canonical URL AND is held as owner. Coverage is decided in SQL
    # (migration 0010) so there is one definition of it.
    ownership_recorded = False
    if service == "search_console":
        evidence = await repo.ownership_evidence(website_id)
        if evidence is not None:
            await repo.record_ownership(
                website_id, evidence["property_id"], "search_console"
            )
            ownership_recorded = True

    await events.publish(
        events.DomainEvent(
            events.PROPERTY_CONNECTED,
            {
                "website_id": str(website_id),
                "service": service,
                "property_uri": prop["property_uri"],
            },
        )
    )

    return LinkOut(
        id=str(link["id"]),
        website_id=str(website_id),
        service=service,
        property_uri=prop["property_uri"],
        link_method=link_method,
        ownership_recorded=ownership_recorded,
    )


class ConnectIn(BaseModel):
    website_id: UUID
    link_method: str = "user_selected"


@router.post("/search-console/properties/{property_id}/connect", response_model=LinkOut)
async def connect_search_console(
    property_id: Annotated[UUID, Path()],
    body: ConnectIn,
    memberships: MembershipsDep,
    websites: WebsiteRepoDep,
    conn: ServiceConnectionDep,
) -> LinkOut:
    return await _connect(
        "search_console",
        property_id,
        body.website_id,
        memberships,
        websites,
        conn,
        body.link_method,
    )


@router.post("/analytics/properties/{property_id}/connect", response_model=LinkOut)
async def connect_analytics(
    property_id: Annotated[UUID, Path()],
    body: ConnectIn,
    memberships: MembershipsDep,
    websites: WebsiteRepoDep,
    conn: ServiceConnectionDep,
) -> LinkOut:
    return await _connect(
        "analytics",
        property_id,
        body.website_id,
        memberships,
        websites,
        conn,
        body.link_method,
    )
