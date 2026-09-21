"""The authorization-code flow with PKCE (V1 spec s7).

    Browser -> GET /google/connect      state stored in Redis, 302 to Google
    Google  -> user consents
    Browser -> GET /google/callback     state consumed once, code exchanged

Scopes are requested INCREMENTALLY: identity and Search Console on the first
pass, Analytics only when the user opts in. Asking for everything up front
depresses consent conversion and widens the verification surface for features
that may not exist yet.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

from api.hub.providers.google.client import AUTH_ENDPOINT

SCOPE_OPENID = ["openid", "email", "profile"]
SCOPE_SEARCH_CONSOLE = ["https://www.googleapis.com/auth/webmasters.readonly"]
SCOPE_ANALYTICS = ["https://www.googleapis.com/auth/analytics.readonly"]

SERVICE_SCOPES = {
    "search_console": SCOPE_SEARCH_CONSOLE,
    "analytics": SCOPE_ANALYTICS,
}


@dataclass(frozen=True, slots=True)
class Pkce:
    verifier: str
    challenge: str
    method: str = "S256"


def generate_pkce() -> Pkce:
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return Pkce(verifier=verifier, challenge=challenge)


def scopes_for(services: list[str]) -> list[str]:
    scopes = list(SCOPE_OPENID)
    for service in services:
        scopes.extend(SERVICE_SCOPES.get(service, []))
    # Stable order so the consent screen is the same every time and the stored
    # grant is comparable between connects.
    seen: set[str] = set()
    ordered: list[str] = []
    for scope in scopes:
        if scope not in seen:
            seen.add(scope)
            ordered.append(scope)
    return ordered


def authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state_id: str,
    challenge: str,
    has_existing_refresh_token: bool,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state_id,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        # Widening a grant later must not discard what was already granted.
        "include_granted_scopes": "true",
    }
    if not has_existing_refresh_token:
        # Only force the consent screen when we have no refresh token to fall
        # back on. Sending prompt=consent every time re-prompts returning users
        # for nothing, which is a common and very visible bug.
        params["prompt"] = "consent"
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def granted_covers(requested: list[str], granted: list[str]) -> dict[str, bool]:
    """Which services the user actually granted.

    Google lets a user untick individual permissions, so the granted set can be
    narrower than the requested one. Features gate on this, never on what was
    asked for.
    """
    granted_set = set(granted)
    return {
        service: all(scope in granted_set for scope in scopes)
        for service, scopes in SERVICE_SCOPES.items()
        if any(scope in requested for scope in scopes)
    }
