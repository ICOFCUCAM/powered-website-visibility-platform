"""M1 acceptance criterion (docs/09-mvp-sequence.md):

    Two users in two orgs cannot see each other's websites, proven by a test
    that queries as each and asserts empty.

These go through the real HTTP surface and a real database with RLS active, so
they exercise the handler filter AND the policy backstop together.
"""

from __future__ import annotations

import uuid

from api.tests.conftest import auth_headers


async def test_a_user_sees_only_their_own_organisations_websites(client, two_tenants):
    a, b = two_tenants["user_a"], two_tenants["user_b"]
    slug = two_tenants["slug"]

    created = await client.post(
        "/api/v1/websites",
        json={"url": f"https://a-{slug}.example.com"},
        headers=auth_headers(a),
    )
    assert created.status_code == 201, created.text
    website_id = created.json()["id"]

    mine = await client.get("/api/v1/websites", headers=auth_headers(a))
    assert [w["id"] for w in mine.json()] == [website_id]

    theirs = await client.get("/api/v1/websites", headers=auth_headers(b))
    assert theirs.json() == []


async def test_changing_the_uuid_in_the_url_does_not_cross_tenants(client, two_tenants):
    a, b = two_tenants["user_a"], two_tenants["user_b"]
    slug = two_tenants["slug"]

    created = await client.post(
        "/api/v1/websites",
        json={"url": f"https://x-{slug}.example.com"},
        headers=auth_headers(a),
    )
    website_id = created.json()["id"]

    # The owner can read it.
    assert (
        await client.get(f"/api/v1/websites/{website_id}", headers=auth_headers(a))
    ).status_code == 200

    # The other tenant gets 404, not 403: a distinct 403 would confirm that
    # this website exists to anyone guessing UUIDs.
    other = await client.get(
        f"/api/v1/websites/{website_id}", headers=auth_headers(b)
    )
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "not_found"

    # And a UUID that exists nowhere is indistinguishable from one that does.
    missing = await client.get(
        f"/api/v1/websites/{uuid.uuid4()}", headers=auth_headers(b)
    )
    assert missing.status_code == other.status_code
    assert missing.json()["error"]["code"] == other.json()["error"]["code"]


async def test_the_other_tenant_cannot_delete_it_either(client, two_tenants):
    a, b = two_tenants["user_a"], two_tenants["user_b"]
    slug = two_tenants["slug"]

    created = await client.post(
        "/api/v1/websites",
        json={"url": f"https://d-{slug}.example.com"},
        headers=auth_headers(a),
    )
    website_id = created.json()["id"]

    assert (
        await client.delete(
            f"/api/v1/websites/{website_id}", headers=auth_headers(b)
        )
    ).status_code == 404

    still_there = await client.get(
        f"/api/v1/websites/{website_id}", headers=auth_headers(a)
    )
    assert still_there.status_code == 200


async def test_unauthenticated_and_forged_tokens_are_refused(client, two_tenants):
    assert (await client.get("/api/v1/websites")).status_code == 401

    bad = await client.get(
        "/api/v1/websites", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "not_authenticated"

    import jwt as pyjwt

    forged = pyjwt.encode(
        {"sub": str(two_tenants["user_a"]), "exp": 9999999999},
        "a-different-secret-also-32-bytes-long-xx",
        algorithm="HS256",
    )
    signed_elsewhere = await client.get(
        "/api/v1/websites", headers={"Authorization": f"Bearer {forged}"}
    )
    assert signed_elsewhere.status_code == 401


async def test_me_reports_only_the_callers_memberships(client, two_tenants):
    response = await client.get(
        "/api/v1/auth/me", headers=auth_headers(two_tenants["user_a"])
    )
    body = response.json()
    assert body["user_id"] == str(two_tenants["user_a"])
    assert [o["organization_id"] for o in body["organizations"]] == [
        str(two_tenants["org_a"])
    ]
