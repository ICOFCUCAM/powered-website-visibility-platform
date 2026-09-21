"""Connect and callback (V1 spec s30: /google/connect, /google/callback)."""

from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from api.deps import MembershipsDep, PrincipalDep
from api.domain.errors import Forbidden
from api.hub.deps import (
    GoogleClientDep,
    GoogleSettingsDep,
    ServiceConnectionDep,
    StateStoreDep,
    TokenVaultDep,
)
from api.hub.providers.google.errors import GoogleAuthorizationFailed
from api.hub.repositories import HubRepository
from api.hub.services import events
from api.hub.services.discovery import DiscoveryService
from api.hub.services.oauth_flow import (
    authorization_url,
    generate_pkce,
    granted_covers,
    scopes_for,
)
from api.hub.services.oauth_state import OAuthState, new_state_id

logger = logging.getLogger("visibility_hub.oauth")

router = APIRouter(prefix="/google", tags=["google"])

DEFAULT_SERVICES = ["search_console"]


# response_model=None: the union return type is two Response classes,
# which FastAPI would otherwise try to turn into a Pydantic schema.
@router.get("/connect", response_model=None)
async def connect(
    request: Request,
    principal: PrincipalDep,
    memberships: MembershipsDep,
    settings: GoogleSettingsDep,
    states: StateStoreDep,
    conn: ServiceConnectionDep,
    website_id: Annotated[UUID | None, Query()] = None,
    services: Annotated[list[str] | None, Query()] = None,
) -> RedirectResponse | JSONResponse:
    """Starts the flow.

    Returns a 302 by default, per the V1 spec. But a browser cannot send an
    Authorization header on a plain link or a redirect, so a single-page app
    cannot start the flow that way. Asking for JSON returns the URL instead and
    the app navigates to it itself.

    The alternative — putting the session token in the query string so a link
    works — writes a credential into browser history, server logs and the
    Referer header. Not worth it for one extra fetch.
    """
    writable = [m for m in memberships if m.role.can_write]
    if not writable:
        raise Forbidden("Your role doesn't allow connecting Google.")
    membership = writable[0]

    requested = services or DEFAULT_SERVICES
    pkce = generate_pkce()
    state_id = new_state_id()

    await states.put(
        state_id,
        OAuthState(
            organization_id=str(membership.organization_id),
            user_id=str(principal.user_id),
            website_id=str(website_id) if website_id else None,
            code_verifier=pkce.verifier,
            services=requested,
            redirect_after=f"{settings.web_base_url}/onboarding/properties",
        ),
    )

    # Only force the consent screen when this organisation has no working
    # refresh token to fall back on.
    repo = HubRepository(conn)
    existing = [
        c
        for c in await repo.list_connections(membership.organization_id)
        if c["status"] == "active"
    ]

    url = authorization_url(
        client_id=settings.client_id,
        redirect_uri=settings.redirect_uri,
        scopes=scopes_for(requested),
        state_id=state_id,
        challenge=pkce.challenge,
        has_existing_refresh_token=bool(existing),
    )

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"authorization_url": url})
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
async def callback(
    settings: GoogleSettingsDep,
    states: StateStoreDep,
    client: GoogleClientDep,
    conn: ServiceConnectionDep,
    vault: TokenVaultDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """Completes the authorization and discovers what the account can see.

    Errors redirect into the wizard with a code rather than rendering an API
    error page: the user is in a browser flow, and dropping them on JSON is how
    a connect failure becomes a support ticket.
    """
    if error or not code or not state:
        return _back(settings.web_base_url, "google_auth_failed")

    # Single use: GETDEL, so a replayed callback cannot bind a second
    # connection.
    stored = await states.take(state)
    if stored is None:
        return _back(settings.web_base_url, "google_state_expired")

    try:
        tokens = await client.exchange_code(
            code, stored.code_verifier, settings.redirect_uri
        )
    except GoogleAuthorizationFailed:
        return _back(settings.web_base_url, "google_auth_failed")

    if not tokens.subject:
        return _back(settings.web_base_url, "google_auth_failed")

    organization_id = UUID(stored.organization_id)
    repo = HubRepository(conn)

    refresh_token_id = None
    if tokens.refresh_token:
        refresh_token_id = await vault.store(tokens.refresh_token)

    connection = await repo.upsert_connection(
        organization_id=organization_id,
        provider_key="google",
        external_id=tokens.subject,
        label=tokens.email or tokens.subject,
        granted_scopes=tokens.scopes,
        refresh_token_id=refresh_token_id,
        connected_by=UUID(stored.user_id),
    )

    # The user may have unticked individual permissions, so discovery runs only
    # for what was actually granted.
    coverage = granted_covers(scopes_for(stored.services), tokens.scopes)
    granted_services = [s for s, ok in coverage.items() if ok]

    result = await DiscoveryService(repo, client).discover(
        organization_id=organization_id,
        connection_id=connection["id"],
        access_token=tokens.access_token,
        services=granted_services,
    )
    logger.info(
        "discovery search_console=%s analytics=%s",
        result.search_console,
        result.analytics,
    )

    await events.publish(
        events.DomainEvent(
            events.SYNC_COMPLETED,
            {
                "connection_id": str(connection["id"]),
                "organization_id": str(organization_id),
                "kind": "discovery",
            },
        )
    )

    params = {"connected": "google"}
    if stored.website_id:
        params["website_id"] = stored.website_id
    denied = [s for s, ok in coverage.items() if not ok]
    if denied:
        params["missing"] = ",".join(denied)

    return RedirectResponse(
        f"{stored.redirect_after}?{urlencode(params)}", status_code=302
    )


def _back(web_base_url: str, code: str) -> RedirectResponse:
    return RedirectResponse(
        f"{web_base_url}/onboarding/google?{urlencode({'error': code})}",
        status_code=302,
    )
