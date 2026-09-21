"""Website onboarding through the API."""

from __future__ import annotations

from api.tests.conftest import auth_headers


async def test_the_user_can_type_anything_reasonable(client, two_tenants):
    a, slug = two_tenants["user_a"], two_tenants["slug"]

    response = await client.post(
        "/api/v1/websites",
        json={"url": f"  HTTPS://WWW.Typed-{slug}.example.com/about?ref=x  "},
        headers=auth_headers(a),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["domain"] == f"typed-{slug}.example.com"
    assert body["canonical_url"] == f"https://www.typed-{slug}.example.com"
    assert body["status"] == "PENDING"


async def test_a_new_website_is_not_crawlable_until_ownership_is_verified(
    client, two_tenants
):
    a, slug = two_tenants["user_a"], two_tenants["slug"]

    created = await client.post(
        "/api/v1/websites",
        json={"url": f"https://new-{slug}.example.com"},
        headers=auth_headers(a),
    )
    website_id = created.json()["id"]
    assert created.json()["crawl_allowed"] is False

    eligibility = await client.get(
        f"/api/v1/websites/{website_id}/crawl-eligibility", headers=auth_headers(a)
    )
    assert eligibility.json() == {
        "allowed": False,
        "reason": "ownership_not_verified",
    }


async def test_adding_the_same_website_twice_is_refused(client, two_tenants):
    a, slug = two_tenants["user_a"], two_tenants["slug"]
    url = f"https://dupe-{slug}.example.com"

    first = await client.post(
        "/api/v1/websites", json={"url": url}, headers=auth_headers(a)
    )
    assert first.status_code == 201

    # Typed differently, same website.
    second = await client.post(
        "/api/v1/websites",
        json={"url": f"http://www.dupe-{slug}.example.com/pricing"},
        headers=auth_headers(a),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "website_already_exists"


async def test_rejects_an_address_that_is_not_a_website(client, two_tenants):
    response = await client.post(
        "/api/v1/websites",
        json={"url": "http://127.0.0.1:8000"},
        headers=auth_headers(two_tenants["user_a"]),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_website"


async def test_the_free_plan_cap_is_enforced_at_admission(client, two_tenants):
    """The default plan allows one website. The second is refused by the cap,
    not by a UI that hides the button."""
    a, slug = two_tenants["user_a"], two_tenants["slug"]

    first = await client.post(
        "/api/v1/websites",
        json={"url": f"https://cap1-{slug}.example.com"},
        headers=auth_headers(a),
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/websites",
        json={"url": f"https://cap2-{slug}.example.com"},
        headers=auth_headers(a),
    )
    assert second.status_code == 402
    body = second.json()["error"]
    assert body["code"] == "plan_limit_exceeded"
    assert body["details"]["limit"] == 1


async def test_a_client_cannot_set_derived_fields_by_sending_them(client, two_tenants):
    """crawl_allowed and organization_id are not inputs. Sending them changes
    nothing — there is no field to bind them to."""
    a, slug = two_tenants["user_a"], two_tenants["slug"]

    response = await client.post(
        "/api/v1/websites",
        json={
            "url": f"https://inject-{slug}.example.com",
            "crawl_allowed": True,
            "organization_id": str(two_tenants["org_b"]),
            "ownership_verified_at": "2020-01-01T00:00:00Z",
        },
        headers=auth_headers(a),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["crawl_allowed"] is False
    assert body["organization_id"] == str(two_tenants["org_a"])
    assert body["ownership_verified"] is False
