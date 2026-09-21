"""Row-level security as defence in depth.

Every repository query carries its own tenant filter. These tests check what
happens when one does NOT — the case a future handler will eventually hit by
accident. If RLS is live, the unfiltered query still returns nothing that
belongs to another organisation.

This exists because the claim is easy to make and easy to get wrong: connect
the request path as the database owner or a superuser and every policy in
migration 0006 silently becomes decorative, while the tests keep passing
because the handlers filter correctly anyway.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from api.adapters import db
from api.tests.conftest import auth_headers


async def _seed_website(client, user, url: str) -> str:
    response = await client.post(
        "/api/v1/websites", json={"url": url}, headers=auth_headers(user)
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_the_request_path_role_does_not_bypass_rls(client):
    """If this fails, every other tenancy test is proving nothing."""
    async with db.session() as conn:
        row = await (
            await conn.execute(
                "select current_user as role, "
                "       (select rolbypassrls from pg_roles "
                "         where rolname = current_user) as bypasses"
            )
        ).fetchone()

    assert row["bypasses"] is False, (
        f"the API connects as {row['role']}, which bypasses row-level security; "
        "RLS policies are decorative under that role"
    )


async def test_an_unfiltered_query_still_cannot_see_another_tenant(
    client, two_tenants
):
    slug = two_tenants["slug"]
    await _seed_website(client, two_tenants["user_a"], f"https://rls-a-{slug}.example.com")
    await _seed_website(client, two_tenants["user_b"], f"https://rls-b-{slug}.example.com")

    # Deliberately no `where organization_id = ...`: this is the bug we are
    # defending against, written out explicitly.
    async with db.session(two_tenants["user_a"]) as conn:
        rows = await (
            await conn.execute("select domain::text as domain from websites")
        ).fetchall()

    domains = {r["domain"] for r in rows}
    assert f"rls-a-{slug}.example.com" in domains
    assert f"rls-b-{slug}.example.com" not in domains


async def test_an_unbound_session_sees_nothing_at_all(client, two_tenants):
    """A connection with no `app.user_id` is not a privileged connection.

    A background job that forgets to bind a user must fail closed, not read
    every tenant's data.
    """
    slug = two_tenants["slug"]
    await _seed_website(client, two_tenants["user_a"], f"https://rls-c-{slug}.example.com")

    async with db.session() as conn:
        rows = await (await conn.execute("select id from websites")).fetchall()

    assert rows == []


async def test_the_request_path_role_cannot_read_the_token_vault(client):
    """`secrets` has no grant to the request-path role, so a compromised
    client key cannot reach a customer's Google refresh token."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with db.session() as conn:
            await conn.execute("select count(*) from secrets.oauth_tokens")


async def test_the_service_role_can_reach_the_vault_and_is_separate(client):
    service_dsn = os.environ.get("SERVICE_DATABASE_URL")
    if not service_dsn or service_dsn == os.environ.get("DATABASE_URL"):
        pytest.skip("SERVICE_DATABASE_URL is not configured separately")

    async with db.service_session() as conn:
        row = await (
            await conn.execute(
                "select current_user as role, count(*) as n from secrets.oauth_tokens"
            )
        ).fetchone()

    assert row["role"] != "postgres", "the service role must not be a superuser"
    assert row["n"] >= 0
