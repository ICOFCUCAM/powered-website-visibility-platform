"""Linking a property to a website, and what that does or does not prove."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from api.tests.conftest import auth_headers
from api.tests.fake_google import FakeGoogle, ga4_account, site


async def _website(client, user, url: str) -> str:
    r = await client.post(
        "/api/v1/websites", json={"url": url}, headers=auth_headers(user)
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _connect(client, user, website_id, services=("search_console",)):
    query = "&".join(f"services={s}" for s in services)
    start = await client.get(
        f"/api/v1/google/connect?website_id={website_id}&{query}",
        headers=auth_headers(user),
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return await client.get(f"/api/v1/google/callback?code=c&state={state}")


async def _properties(client, user, website_id, service="search-console"):
    r = await client.get(
        f"/api/v1/google/{service}/properties?website_id={website_id}",
        headers=auth_headers(user),
    )
    return r.json()


@pytest.mark.parametrize(
    "google", [FakeGoogle(sites=[site("sc-domain:example.com")])], indirect=True
)
async def test_linking_a_covering_owned_property_records_ownership(
    client, two_tenants, google
):
    user = two_tenants["user_a"]
    website_id = await _website(client, user, "https://example.com")
    await _connect(client, user, website_id)

    prop = (await _properties(client, user, website_id))[0]
    linked = await client.post(
        f"/api/v1/google/search-console/properties/{prop['id']}/connect",
        json={"website_id": website_id},
        headers=auth_headers(user),
    )
    assert linked.status_code == 200
    assert linked.json()["ownership_recorded"] is True

    website = await client.get(
        f"/api/v1/websites/{website_id}", headers=auth_headers(user)
    )
    assert website.json()["ownership_verified"] is True

    eligibility = await client.get(
        f"/api/v1/websites/{website_id}/crawl-eligibility", headers=auth_headers(user)
    )
    assert eligibility.json() == {"allowed": True, "reason": None}


@pytest.mark.parametrize(
    "google",
    [FakeGoogle(sites=[site("sc-domain:example.com", "siteRestrictedUser")])],
    indirect=True,
)
async def test_a_readable_property_is_not_proof_of_ownership(
    client, two_tenants, google
):
    """siteRestrictedUser can read the data but is not an owner. We will sync
    it; we will not treat it as permission to crawl."""
    user = two_tenants["user_a"]
    website_id = await _website(client, user, "https://example.com")
    await _connect(client, user, website_id)

    prop = (await _properties(client, user, website_id))[0]
    assert prop["proves_ownership"] is False

    linked = await client.post(
        f"/api/v1/google/search-console/properties/{prop['id']}/connect",
        json={"website_id": website_id},
        headers=auth_headers(user),
    )
    assert linked.json()["ownership_recorded"] is False

    eligibility = await client.get(
        f"/api/v1/websites/{website_id}/crawl-eligibility", headers=auth_headers(user)
    )
    assert eligibility.json()["reason"] == "ownership_not_verified"


@pytest.mark.parametrize(
    "google",
    [FakeGoogle(sites=[site("https://www.example.com/")])],
    indirect=True,
)
async def test_a_url_prefix_property_does_not_cover_a_different_scheme(
    client, two_tenants, google
):
    """The website is http://; the property is https://. It matches by host and
    covers nothing the crawler is about to fetch."""
    user = two_tenants["user_a"]
    website_id = await _website(client, user, "http://www.example.com")
    await _connect(client, user, website_id)

    prop = (await _properties(client, user, website_id))[0]
    linked = await client.post(
        f"/api/v1/google/search-console/properties/{prop['id']}/connect",
        json={"website_id": website_id},
        headers=auth_headers(user),
    )
    assert linked.json()["ownership_recorded"] is False


@pytest.mark.parametrize(
    "google",
    [
        FakeGoogle(
            sites=[site("sc-domain:example.com"), site("https://example.com/")]
        )
    ],
    indirect=True,
)
async def test_relinking_replaces_the_active_link_and_keeps_the_history(
    client, two_tenants, google, service_conn
):
    user = two_tenants["user_a"]
    website_id = await _website(client, user, "https://example.com")
    await _connect(client, user, website_id)
    props = await _properties(client, user, website_id)

    for prop in props[:2]:
        await client.post(
            f"/api/v1/google/search-console/properties/{prop['id']}/connect",
            json={"website_id": website_id},
            headers=auth_headers(user),
        )

    rows = await (
        await service_conn.execute(
            "select status, count(*) as n from website_connections "
            " where website_id = %s group by status order by status",
            (website_id,),
        )
    ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    assert counts["active"] == 1
    # The previous link is kept, not deleted: what fed a website's numbers
    # must survive a reconnection.
    assert counts["unlinked"] == 1


async def test_another_tenant_cannot_link_your_property(client, two_tenants, google):
    a, b = two_tenants["user_a"], two_tenants["user_b"]
    website_id = await _website(client, a, f"https://t-{two_tenants['slug']}.example.com")

    stolen = await client.post(
        "/api/v1/google/search-console/properties/"
        "00000000-0000-0000-0000-000000000001/connect",
        json={"website_id": website_id},
        headers=auth_headers(b),
    )
    assert stolen.status_code == 404


@pytest.mark.parametrize(
    "google",
    [
        FakeGoogle(
            scopes=[
                "openid",
                "email",
                "profile",
                "https://www.googleapis.com/auth/webmasters.readonly",
                "https://www.googleapis.com/auth/analytics.readonly",
            ],
            sites=[site("sc-domain:example.com")],
            analytics_accounts=[
                ga4_account("Acme", [("properties/111", "Example — GA4"),
                                     ("properties/222", "Someone else")])
            ],
            stream_urls={
                "properties/111": ["https://www.example.com"],
                "properties/222": ["https://unrelated.test"],
            },
        )
    ],
    indirect=True,
)
async def test_analytics_properties_are_matched_through_their_streams(
    client, two_tenants, google
):
    user = two_tenants["user_a"]
    website_id = await _website(client, user, "https://example.com")
    await _connect(client, user, website_id, services=("search_console", "analytics"))

    props = await _properties(client, user, website_id, service="analytics")
    by_uri = {p["property_uri"]: p for p in props}

    assert by_uri["properties/111"]["suggested"] is True
    assert by_uri["properties/222"]["suggested"] is False
    # The stream lookup is what makes that possible.
    assert set(google.stream_lookups) == {"properties/111", "properties/222"}
