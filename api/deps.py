"""Request dependency chain.

The rule this file exists to enforce, from the V1 spec (s34-35):

    A handler never accepts an organization_id from the client.

Scope is derived from the authenticated session and the requested resource,
then checked. Changing a UUID in a URL cannot reach another tenant's data, and
there is no query parameter that widens access.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Path, Request
from psycopg import AsyncConnection

from api.adapters import db
from api.adapters.auth_supabase import principal_from_token
from api.config import Settings, get_settings
from api.domain.errors import Forbidden, NotAuthenticated, NotFound
from api.domain.models import Membership, Principal, Role, Website
from api.repositories.postgres.organizations import PostgresOrganizationRepository
from api.repositories.postgres.websites import PostgresWebsiteRepository

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_principal(request: Request, settings: SettingsDep) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise NotAuthenticated()
    return principal_from_token(token, settings)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def get_connection(principal: PrincipalDep) -> AsyncIterator[AsyncConnection]:
    """A connection whose transaction is bound to the caller, so RLS applies."""
    async with db.session(principal.user_id) as conn:
        yield conn


ConnectionDep = Annotated[AsyncConnection, Depends(get_connection)]


def get_websites(conn: ConnectionDep) -> PostgresWebsiteRepository:
    return PostgresWebsiteRepository(conn)


def get_organizations(conn: ConnectionDep) -> PostgresOrganizationRepository:
    return PostgresOrganizationRepository(conn)


WebsiteRepoDep = Annotated[PostgresWebsiteRepository, Depends(get_websites)]
OrgRepoDep = Annotated[PostgresOrganizationRepository, Depends(get_organizations)]


async def get_memberships(
    principal: PrincipalDep, orgs: OrgRepoDep
) -> list[Membership]:
    return await orgs.memberships_for_user(principal.user_id)


MembershipsDep = Annotated[list[Membership], Depends(get_memberships)]


@dataclass(frozen=True, slots=True)
class WebsiteScope:
    """A website the caller provably has access to, with their role on it.

    Handlers take this instead of a website_id, so "did we check?" is answered
    by the signature rather than by reading the body.
    """

    website: Website
    membership: Membership

    @property
    def role(self) -> Role:
        return self.membership.role

    def require_write(self) -> None:
        if not self.role.can_write:
            raise Forbidden("Your role doesn't allow changes.")

    def require_admin(self) -> None:
        if not self.role.can_administer:
            raise Forbidden("Only an owner or admin can do that.")


async def get_website_scope(
    website_id: Annotated[UUID, Path()],
    websites: WebsiteRepoDep,
    memberships: MembershipsDep,
) -> WebsiteScope:
    website = await websites.get(website_id)
    by_org = {m.organization_id: m for m in memberships}

    # Not found and not permitted return the SAME error on purpose. A distinct
    # 403 would confirm the existence of another tenant's website to anyone
    # guessing UUIDs.
    if website is None or website.organization_id not in by_org:
        raise NotFound("We couldn't find that website.")

    return WebsiteScope(website=website, membership=by_org[website.organization_id])


WebsiteScopeDep = Annotated[WebsiteScope, Depends(get_website_scope)]
