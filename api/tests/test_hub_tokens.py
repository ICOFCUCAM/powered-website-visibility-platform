"""Token refresh, re-auth and disconnect.

What happens when a grant dies matters more than the happy path: retrying a
dead grant is how an OAuth client earns a rate limit, and a connection that
silently stops syncing is how a customer loses trust in the charts.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from api.hub.providers.google.client import GoogleClient
from api.hub.providers.google.errors import GoogleRefreshRejected
from api.hub.services.keys import LocalKeyManager
from api.hub.services.tokens import TokenService
from api.hub.services.vault import TokenVault
from api.tests.conftest import auth_headers
from api.tests.fake_google import FakeGoogle, site


async def _connected(client, user, fake, url="https://example.com"):
    created = await client.post(
        "/api/v1/websites", json={"url": url}, headers=auth_headers(user)
    )
    assert created.status_code == 201, created.text
    website_id = created.json()["id"]
    start = await client.get(
        f"/api/v1/google/connect?website_id={website_id}", headers=auth_headers(user)
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    await client.get(f"/api/v1/google/callback?code=c&state={state}")
    return website_id


async def _website_id(client, user) -> str:
    listed = await client.get("/api/v1/websites", headers=auth_headers(user))
    return listed.json()[0]["id"]


def _service(conn, fake: FakeGoogle) -> TokenService:
    import os

    keys = LocalKeyManager.from_base64(os.environ["TOKEN_MASTER_KEY"])
    return TokenService(
        conn,
        TokenVault(conn, keys),
        GoogleClient("id", "secret", http=fake.client()),
    )


async def _connection_id(conn, organization_id):
    row = await (
        await conn.execute(
            "select id from connections where organization_id = %s "
            "order by created_at desc limit 1",
            (organization_id,),
        )
    ).fetchone()
    return row["id"] if row else None


@pytest.mark.parametrize(
    "google", [FakeGoogle(sites=[site("sc-domain:example.com")])], indirect=True
)
async def test_a_refresh_produces_a_usable_access_token(
    client, two_tenants, google, service_conn
):
    await _connected(client, two_tenants["user_a"], google)
    connection_id = await _connection_id(service_conn, two_tenants["org_a"])

    service = _service(service_conn, google)
    token = await service.access_token_for(connection_id)
    assert token == "access-refreshed"

    # Cached: a second call in the same request does not spend another refresh.
    assert await service.access_token_for(connection_id) == token


@pytest.mark.parametrize(
    "google", [FakeGoogle(sites=[site("sc-domain:example.com")])], indirect=True
)
async def test_a_rotated_refresh_token_replaces_the_stored_one(
    client, two_tenants, google, service_conn
):
    """Google sometimes issues a replacement. Keeping the old one would
    guarantee a later failure that looks like a revocation."""
    await _connected(client, two_tenants["user_a"], google)
    connection_id = await _connection_id(service_conn, two_tenants["org_a"])

    rotating = FakeGoogle(rotate_refresh_token_to="refresh-token-2")
    await _service(service_conn, rotating).access_token_for(connection_id)

    row = await (
        await service_conn.execute(
            "select refresh_token_id from connections where id = %s", (connection_id,)
        )
    ).fetchone()
    import os

    vault = TokenVault(
        service_conn, LocalKeyManager.from_base64(os.environ["TOKEN_MASTER_KEY"])
    )
    assert await vault.read(row["refresh_token_id"]) == "refresh-token-2"


@pytest.mark.parametrize(
    "google", [FakeGoogle(sites=[site("sc-domain:example.com")])], indirect=True
)
async def test_invalid_grant_marks_the_connection_for_reauth(
    client, two_tenants, google, service_conn
):
    """`invalid_grant` is terminal: revoked, password changed, or six months
    unused. Scheduling stops and the UI shows a reconnect card."""
    user = two_tenants["user_a"]
    await _connected(client, user, google)
    connection_id = await _connection_id(service_conn, two_tenants["org_a"])

    dead = FakeGoogle(refresh_invalid_grant=True)
    with pytest.raises(GoogleRefreshRejected):
        await _service(service_conn, dead).access_token_for(connection_id)

    row = await (
        await service_conn.execute(
            "select status, last_error from connections where id = %s",
            (connection_id,),
        )
    ).fetchone()
    assert row["status"] == "needs_reauth"
    assert row["last_error"] == "invalid_grant"

    listed = await client.get(
        "/api/v1/google/connections", headers=auth_headers(user)
    )
    assert listed.json()[0]["status"] == "needs_reauth"


@pytest.mark.parametrize(
    "google", [FakeGoogle(sites=[site("sc-domain:example.com")])], indirect=True
)
async def test_disconnecting_revokes_with_google_and_destroys_the_secret(
    client, two_tenants, google, service_conn
):
    """Disconnect must actually disconnect (V1 spec §37)."""
    user = two_tenants["user_a"]
    await _connected(client, user, google)

    listed = await client.get("/api/v1/google/connections", headers=auth_headers(user))
    connection_id = listed.json()[0]["id"]

    before = await (
        await service_conn.execute("select count(*) as n from secrets.oauth_tokens")
    ).fetchone()

    response = await client.delete(
        f"/api/v1/google/connections/{connection_id}", headers=auth_headers(user)
    )
    assert response.status_code == 204

    assert google.revoked == ["refresh-token-1"]

    after = await (
        await service_conn.execute("select count(*) as n from secrets.oauth_tokens")
    ).fetchone()
    assert after["n"] == before["n"] - 1

    row = await (
        await service_conn.execute(
            "select status, refresh_token_id from connections where id = %s",
            (connection_id,),
        )
    ).fetchone()
    assert row["status"] == "revoked"
    assert row["refresh_token_id"] is None

    assert (
        await client.get("/api/v1/google/connections", headers=auth_headers(user))
    ).json() == []


@pytest.mark.parametrize(
    "google", [FakeGoogle(sites=[site("sc-domain:example.com")])], indirect=True
)
async def test_another_tenant_cannot_disconnect_your_google_account(
    client, two_tenants, google
):
    """This route runs on the service connection, which bypasses RLS, so the
    tenant check has to be explicit — and tested."""
    await _connected(client, two_tenants["user_a"], google)
    listed = await client.get(
        "/api/v1/google/connections", headers=auth_headers(two_tenants["user_a"])
    )
    connection_id = listed.json()[0]["id"]

    stolen = await client.delete(
        f"/api/v1/google/connections/{connection_id}",
        headers=auth_headers(two_tenants["user_b"]),
    )
    assert stolen.status_code == 404
    assert google.revoked == []


@pytest.mark.parametrize(
    "google", [FakeGoogle(sites=[site("sc-domain:example.com")])], indirect=True
)
async def test_reconnecting_without_a_new_refresh_token_keeps_the_old_one(
    client, two_tenants, google, service_conn
):
    """Google returns no refresh token when the user re-consents to a grant
    they already gave. Overwriting the stored one with NULL would break sync."""
    user = two_tenants["user_a"]
    await _connected(client, user, google)
    connection_id = await _connection_id(service_conn, two_tenants["org_a"])
    before = await (
        await service_conn.execute(
            "select refresh_token_id from connections where id = %s", (connection_id,)
        )
    ).fetchone()

    # A re-consent for a grant already given: Google returns no refresh token.
    google.refresh_token = None
    website_id = await _website_id(client, user)
    start = await client.get(
        f"/api/v1/google/connect?website_id={website_id}", headers=auth_headers(user)
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    await client.get(f"/api/v1/google/callback?code=c&state={state}")

    after = await (
        await service_conn.execute(
            "select refresh_token_id, status from connections where id = %s",
            (connection_id,),
        )
    ).fetchone()
    assert after["refresh_token_id"] == before["refresh_token_id"]
    assert after["status"] == "active"
