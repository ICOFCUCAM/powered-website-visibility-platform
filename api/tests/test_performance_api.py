"""The read path: Postgres only, and honest about what Google withheld."""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from api.tests.conftest import auth_headers
from api.tests.fake_google import FakeGoogle, site

DAY = (date.today() - timedelta(days=4)).isoformat()
PRIOR = (date.today() - timedelta(days=35)).isoformat()


def _google() -> FakeGoogle:
    return FakeGoogle(
        sites=[site("sc-domain:example.com")],
        search_analytics={
            ("date",): [
                ((DAY,), 100, 5000, 8.4),
                ((PRIOR,), 50, 4000, 10.0),
            ],
            ("date", "query"): [
                # Position 3 on 1,000 impressions and position 20 on 10:
                # weighted 3.17, naively averaged 11.5.
                ((DAY, "church in london"), 30, 1000, 3.0),
                ((DAY, "african church london"), 0, 10, 20.0),
                ((DAY, "sunday worship"), 10, 900, 12.0),
            ],
            ("date", "page"): [
                ((DAY, "https://example.com/"), 25, 2500, 6.0),
                ((DAY, "https://example.com/services"), 15, 1200, 9.5),
            ],
            ("date", "query", "page"): [],
        },
    )


async def _synced(client, user) -> str:
    created = await client.post(
        "/api/v1/websites",
        json={"url": "https://example.com"},
        headers=auth_headers(user),
    )
    website_id = created.json()["id"]
    start = await client.get(
        f"/api/v1/google/connect?website_id={website_id}", headers=auth_headers(user)
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    await client.get(f"/api/v1/google/callback?code=c&state={state}")
    props = await client.get(
        f"/api/v1/google/search-console/properties?website_id={website_id}",
        headers=auth_headers(user),
    )
    await client.post(
        f"/api/v1/google/search-console/properties/{props.json()[0]['id']}/connect",
        json={"website_id": website_id},
        headers=auth_headers(user),
    )
    await client.post(
        f"/api/v1/websites/{website_id}/sync/search-console", headers=auth_headers(user)
    )
    return website_id


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_totals_come_from_the_reconciliation_authority(
    client, two_tenants, google
):
    """100 clicks, as Search Console would show — not the 40 the query rows
    account for."""
    user = two_tenants["user_a"]
    website_id = await _synced(client, user)

    response = await client.get(
        f"/api/v1/websites/{website_id}/search-performance", headers=auth_headers(user)
    )
    body = response.json()
    assert body["totals"]["clicks"] == 100
    assert body["totals"]["impressions"] == 5000


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_every_sliced_response_explains_the_gap(client, two_tenants, google):
    user = two_tenants["user_a"]
    website_id = await _synced(client, user)

    for path in ("queries", "pages", "search-performance"):
        response = await client.get(
            f"/api/v1/websites/{website_id}/{path}", headers=auth_headers(user)
        )
        anon = response.json()["anonymised"]
        assert anon["total_clicks"] == 100
        assert anon["anonymised_clicks"] == 60
        assert "withholds" in anon["note"]


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_position_is_impressions_weighted(client, two_tenants, google):
    """(3x1000 + 20x10 + 12x900) / 1910 = 7.33 weighted by impressions.
    The naive average of 3.0, 20.0 and 12.0 is 11.67 — and the naive figure is
    the one that looks entirely plausible in a dashboard."""
    user = two_tenants["user_a"]
    website_id = await _synced(client, user)

    response = await client.get(
        f"/api/v1/websites/{website_id}/queries?order_by=impressions",
        headers=auth_headers(user),
    )
    rows = {r["label"]: r for r in response.json()["rows"]}
    assert rows["church in london"]["position"] == pytest.approx(3.0)
    assert rows["african church london"]["position"] == pytest.approx(20.0)

    combined = sum(r["position"] * r["impressions"] for r in rows.values())
    weighted = combined / sum(r["impressions"] for r in rows.values())
    assert weighted == pytest.approx(7.33, abs=0.01)
    naive = sum(r["position"] for r in rows.values()) / len(rows)
    assert naive == pytest.approx(11.67, abs=0.01)


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_sorting_by_position_puts_the_best_first(client, two_tenants, google):
    """Position 1 is the best. "Order by position" meaning worst-first would
    surprise everyone who has used Search Console."""
    user = two_tenants["user_a"]
    website_id = await _synced(client, user)

    response = await client.get(
        f"/api/v1/websites/{website_id}/queries?order_by=position",
        headers=auth_headers(user),
    )
    positions = [r["position"] for r in response.json()["rows"]]
    assert positions == sorted(positions)


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_the_comparison_window_inverts_position_direction(
    client, two_tenants, google
):
    """Position improving means the number going down, so the delta is
    inverted once here rather than in every chart that renders it."""
    user = two_tenants["user_a"]
    website_id = await _synced(client, user)

    response = await client.get(
        f"/api/v1/websites/{website_id}/search-performance?compare=true",
        headers=auth_headers(user),
    )
    body = response.json()
    assert body["compared_to"] is not None
    # Clicks rose from 50 to 100 in the prior window.
    assert body["delta"]["clicks"] == pytest.approx(100.0)
    # Position moved 10.0 -> 8.4, an improvement, so the delta is positive.
    assert body["delta"]["position"] > 0


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_the_read_path_never_calls_google(client, two_tenants, google):
    """The property this whole architecture is built around."""
    user = two_tenants["user_a"]
    website_id = await _synced(client, user)
    google.search_analytics_calls.clear()

    for path in ("search-performance", "queries", "pages"):
        await client.get(
            f"/api/v1/websites/{website_id}/{path}", headers=auth_headers(user)
        )

    assert google.search_analytics_calls == []
    assert google.token_requests[-1]["grant_type"] == "refresh_token"


async def test_another_tenant_cannot_read_your_performance(client, two_tenants, google):
    user, other = two_tenants["user_a"], two_tenants["user_b"]
    created = await client.post(
        "/api/v1/websites",
        json={"url": f"https://p-{two_tenants['slug']}.example.com"},
        headers=auth_headers(user),
    )
    website_id = created.json()["id"]

    response = await client.get(
        f"/api/v1/websites/{website_id}/search-performance",
        headers=auth_headers(other),
    )
    assert response.status_code == 404
