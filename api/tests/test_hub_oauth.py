"""The Google Hub, end to end through the real routes.

Everything except Google's own servers is real: real routes, real PKCE, real
state store, real token vault, real database. `api/tests/fake_google.py`
replaces only the transport.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from api.tests.conftest import auth_headers
from api.tests.fake_google import FakeGoogle, site


async def _website(client, user, url: str) -> str:
    response = await client.post(
        "/api/v1/websites", json={"url": url}, headers=auth_headers(user)
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _connect(client, user, website_id: str, services=("search_console",)):
    query = "&".join(f"services={s}" for s in services)
    start = await client.get(
        f"/api/v1/google/connect?website_id={website_id}&{query}",
        headers=auth_headers(user),
    )
    assert start.status_code == 302
    params = parse_qs(urlparse(start.headers["location"]).query)
    callback = await client.get(
        f"/api/v1/google/callback?code=auth-code&state={params['state'][0]}"
    )
    return start, callback


# -- the authorization request -------------------------------------------


async def test_the_authorization_request_is_built_correctly(
    client, two_tenants, google
):
    user = two_tenants["user_a"]
    website_id = await _website(client, user, f"https://c-{two_tenants['slug']}.example.com")

    response = await client.get(
        f"/api/v1/google/connect?website_id={website_id}",
        headers=auth_headers(user),
    )
    assert response.status_code == 302

    target = urlparse(response.headers["location"])
    assert target.netloc == "accounts.google.com"
    params = parse_qs(target.query)

    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["access_type"] == ["offline"]
    assert params["include_granted_scopes"] == ["true"]
    # The verifier must never travel with the code: only the challenge does.
    assert "code_verifier" not in params
    assert len(params["state"][0]) >= 40

    # Incremental authorisation: Search Console only, not Analytics.
    scopes = params["scope"][0].split()
    assert "https://www.googleapis.com/auth/webmasters.readonly" in scopes
    assert "https://www.googleapis.com/auth/analytics.readonly" not in scopes


async def test_analytics_scope_is_only_requested_when_asked_for(
    client, two_tenants, google
):
    user = two_tenants["user_a"]
    response = await client.get(
        "/api/v1/google/connect?services=search_console&services=analytics",
        headers=auth_headers(user),
    )
    scopes = parse_qs(urlparse(response.headers["location"]).query)["scope"][0].split()
    assert "https://www.googleapis.com/auth/analytics.readonly" in scopes


async def test_connecting_requires_authentication(client, google):
    assert (await client.get("/api/v1/google/connect")).status_code == 401


# -- the callback ---------------------------------------------------------


@pytest.mark.parametrize(
    "google",
    [
        FakeGoogle(
            sites=[
                site("sc-domain:example.com"),
                site("https://www.example.com/"),
                site("sc-domain:someone-else.com"),
            ]
        )
    ],
    indirect=True,
)
async def test_a_full_connection_discovers_and_stores(
    client, two_tenants, google, service_conn
):
    user = two_tenants["user_a"]
    website_id = await _website(client, user, "https://example.com")

    _, callback = await _connect(client, user, website_id)
    assert callback.status_code == 302
    assert "onboarding/properties" in callback.headers["location"]

    # The refresh token is sealed, not stored in the clear.
    row = await (
        await service_conn.execute(
            "select ciphertext from secrets.oauth_tokens order by created_at desc limit 1"
        )
    ).fetchone()
    assert b"refresh-token-1" not in bytes(row["ciphertext"])

    # The connection is keyed on Google's subject, not the email.
    conn_row = await (
        await service_conn.execute(
            "select external_id, label::text as label, status from connections "
            "order by created_at desc limit 1"
        )
    ).fetchone()
    assert conn_row["external_id"] == "google-sub-123"
    assert conn_row["label"] == "owner@example.com"
    assert conn_row["status"] == "active"

    # Discovery ran, including the property that is not ours.
    listed = await client.get(
        f"/api/v1/google/search-console/properties?website_id={website_id}",
        headers=auth_headers(user),
    )
    by_uri = {p["property_uri"]: p for p in listed.json()}
    assert by_uri["sc-domain:example.com"]["match_quality"] == "domain"
    assert by_uri["sc-domain:example.com"]["suggested"] is True
    assert by_uri["https://www.example.com/"]["match_quality"] == "host"
    assert by_uri["sc-domain:someone-else.com"]["suggested"] is False

    # Best match first, so the wizard can pre-tick the top row.
    assert listed.json()[0]["property_uri"] == "sc-domain:example.com"


async def test_the_state_is_single_use(client, two_tenants, google):
    """A replayed callback must not bind a second connection."""
    user = two_tenants["user_a"]
    website_id = await _website(client, user, f"https://s-{two_tenants['slug']}.example.com")

    start = await client.get(
        f"/api/v1/google/connect?website_id={website_id}", headers=auth_headers(user)
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    first = await client.get(f"/api/v1/google/callback?code=c&state={state}")
    assert "onboarding/properties" in first.headers["location"]

    replay = await client.get(f"/api/v1/google/callback?code=c&state={state}")
    assert "google_state_expired" in replay.headers["location"]


async def test_an_unknown_state_is_refused(client, google):
    response = await client.get("/api/v1/google/callback?code=c&state=made-up")
    assert response.status_code == 302
    assert "google_state_expired" in response.headers["location"]


async def test_the_user_declining_consent_lands_back_in_the_wizard(client, google):
    """Not a JSON error page: the user is in a browser flow, and dropping them
    on an API response is how a decline becomes a support ticket."""
    response = await client.get("/api/v1/google/callback?error=access_denied")
    assert response.status_code == 302
    assert "onboarding/google" in response.headers["location"]
    assert "google_auth_failed" in response.headers["location"]


@pytest.mark.parametrize("google", [FakeGoogle(exchange_fails=True)], indirect=True)
async def test_a_failed_code_exchange_lands_back_in_the_wizard(
    client, two_tenants, google
):
    user = two_tenants["user_a"]
    _, callback = await _connect(client, user, await _website(
        client, user, f"https://x-{two_tenants['slug']}.example.com"
    ))
    assert "google_auth_failed" in callback.headers["location"]


@pytest.mark.parametrize(
    "google",
    [
        FakeGoogle(
            scopes=["openid", "email", "profile"],
            sites=[site("sc-domain:example.com")],
        )
    ],
    indirect=True,
)
async def test_unticking_a_permission_is_honoured(client, two_tenants, google):
    """Google lets users untick individual permissions, so the granted set can
    be narrower than the requested one. Features gate on what was granted."""
    user = two_tenants["user_a"]
    website_id = await _website(client, user, f"https://u-{two_tenants['slug']}.example.com")

    _, callback = await _connect(client, user, website_id)
    assert "missing=search_console" in callback.headers["location"]

    # Discovery did not run for a service the user did not grant.
    listed = await client.get(
        "/api/v1/google/search-console/properties", headers=auth_headers(user)
    )
    assert listed.json() == []


async def test_a_single_page_app_can_start_the_flow(client, two_tenants, google):
    """A browser cannot put an Authorization header on a link or a redirect,
    so asking for JSON returns the URL for the app to navigate to.

    The alternative — a session token in the query string — writes a
    credential into browser history, server logs and the Referer header.
    """
    response = await client.get(
        "/api/v1/google/connect",
        headers={**auth_headers(two_tenants["user_a"]), "Accept": "application/json"},
    )
    assert response.status_code == 200
    url = response.json()["authorization_url"]
    assert url.startswith("https://accounts.google.com/")

    params = parse_qs(urlparse(url).query)
    assert params["code_challenge_method"] == ["S256"]
    # Same guarantee as the redirect path: the verifier stays server-side.
    assert "code_verifier" not in params


async def test_the_token_never_appears_in_the_authorization_url(
    client, two_tenants, google
):
    response = await client.get(
        "/api/v1/google/connect",
        headers={**auth_headers(two_tenants["user_a"]), "Accept": "application/json"},
    )
    url = response.json()["authorization_url"]
    assert "Bearer" not in url
    assert "eyJ" not in url  # the leading characters of a JWT
