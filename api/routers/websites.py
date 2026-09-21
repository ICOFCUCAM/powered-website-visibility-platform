"""Website routes (V1 spec s30)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from api.deps import MembershipsDep, OrgRepoDep, WebsiteRepoDep, WebsiteScopeDep
from api.domain.crawl_policy import evaluate_crawl_allowed
from api.domain.errors import Forbidden, PlanLimitExceeded, WebsiteAlreadyExists
from api.domain.models import Website
from api.domain.urls import normalise_website_input

router = APIRouter(prefix="/websites", tags=["websites"])


class CreateWebsiteIn(BaseModel):
    # `url` because that is what the user typed; the server decides what it
    # means. There is deliberately no organization_id field — scope comes from
    # the session, never from the client.
    url: str = Field(min_length=3, max_length=2048)
    name: str | None = Field(default=None, max_length=200)


class WebsiteOut(BaseModel):
    id: str
    organization_id: str
    domain: str
    canonical_url: str
    name: str | None
    status: str
    timezone: str
    created_at: datetime
    ownership_verified: bool
    crawl_allowed: bool
    crawl_blocked_reason: str | None

    @classmethod
    def of(cls, w: Website) -> WebsiteOut:
        return cls(
            id=str(w.id),
            organization_id=str(w.organization_id),
            domain=w.domain,
            canonical_url=w.canonical_url,
            name=w.name,
            status=w.status.value,
            timezone=w.timezone,
            created_at=w.created_at,
            ownership_verified=w.ownership_verified,
            crawl_allowed=w.crawl_allowed,
            crawl_blocked_reason=w.crawl_blocked_reason,
        )


@router.get("", response_model=list[WebsiteOut])
async def list_websites(
    websites: WebsiteRepoDep, memberships: MembershipsDep
) -> list[WebsiteOut]:
    out: list[WebsiteOut] = []
    for m in memberships:
        for w in await websites.list_for_organization(m.organization_id):
            out.append(WebsiteOut.of(w))
    return out


@router.post("", response_model=WebsiteOut, status_code=status.HTTP_201_CREATED)
async def create_website(
    body: CreateWebsiteIn,
    websites: WebsiteRepoDep,
    orgs: OrgRepoDep,
    memberships: MembershipsDep,
) -> WebsiteOut:
    writable = [m for m in memberships if m.role.can_write]
    if not writable:
        raise Forbidden("Your role doesn't allow adding websites.")

    # With one organisation this is unambiguous. Multi-org users choose in the
    # UI, which will pass the organisation explicitly once that screen exists —
    # and it will still be checked against membership, never trusted.
    membership = writable[0]

    normalised = normalise_website_input(body.url)

    existing = await websites.find_by_domain(
        membership.organization_id, normalised.domain
    )
    if existing is not None:
        raise WebsiteAlreadyExists(website_id=str(existing.id))

    organization = await orgs.get(membership.organization_id)
    if organization is None:
        raise Forbidden()
    count = await orgs.count_websites(membership.organization_id)
    if count >= organization.max_websites:
        raise PlanLimitExceeded(
            limit=organization.max_websites, plan=organization.plan.value
        )

    website = await websites.create(
        organization_id=membership.organization_id,
        domain=normalised.domain,
        canonical_url=normalised.canonical_url,
        name=body.name,
    )
    return WebsiteOut.of(website)


@router.get("/{website_id}", response_model=WebsiteOut)
async def get_website(scope: WebsiteScopeDep) -> WebsiteOut:
    return WebsiteOut.of(scope.website)


@router.delete("/{website_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_website(scope: WebsiteScopeDep, websites: WebsiteRepoDep) -> Response:
    scope.require_admin()
    await websites.archive(scope.website.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class CrawlEligibilityOut(BaseModel):
    allowed: bool
    reason: str | None


@router.get("/{website_id}/crawl-eligibility", response_model=CrawlEligibilityOut)
async def crawl_eligibility(
    scope: WebsiteScopeDep, websites: WebsiteRepoDep
) -> CrawlEligibilityOut:
    """The live evaluation behind decision 20.

    Note what this does NOT do: read `websites.crawl_allowed`. That column is
    observability. The authority is this evaluation, run again at admission and
    once more by the crawler before it fetches.
    """
    covers = await websites.ownership_covers_canonical_url(scope.website.id)
    decision = evaluate_crawl_allowed(scope.website, ownership_covers_target=covers)
    return CrawlEligibilityOut(allowed=decision.allowed, reason=decision.reason)
