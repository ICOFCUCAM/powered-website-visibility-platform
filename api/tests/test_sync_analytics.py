"""GA4 synchronisation and the goal-mapping step.

The constraint this milestone is really about: GA4 key events are named by
whoever set the property up, so the platform cannot infer which one means "an
enquiry". Until the customer says, outcomes are ABSENT — not zero, and not
assembled from whichever event looked important.
"""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from api.tests.conftest import auth_headers
from api.tests.fake_google import FakeGoogle, ga4_account, site

DAY = date.today() - timedelta(days=3)
GA = DAY.strftime("%Y%m%d")

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"


def _google(**over) -> FakeGoogle:
    base = dict(
        scopes=[
            "openid", "email", "profile",
            "https://www.googleapis.com/auth/webmasters.readonly",
            ANALYTICS_SCOPE,
        ],
        sites=[site("sc-domain:example.com")],
        analytics_accounts=[ga4_account("Acme", [("properties/111", "Example — GA4")])],
        stream_urls={"properties/111": ["https://www.example.com"]},
        ga4_key_events=[
            {
                "eventName": "contact_form_submit",
                "custom": True,
                "countingMethod": "ONCE_PER_EVENT",
            },
            {"eventName": "purchase", "custom": False},
            {"eventName": "scroll", "custom": False},
        ],
        ga4_reports={
            # date -> sessions, users, engaged, rate, views, keyEvents
            ("date",): [(GA, 2400, 1900, 1450, 0.604, 5200, 37)],
            ("date", "pagePath"): [
                (GA, "/", 900, 800, 600, 0.667, 1400, 5),
                (GA, "/services", 500, 460, 380, 0.760, 700, 22),
            ],
            ("date", "sessionDefaultChannelGroup"): [
                (GA, "Organic Search", 1500, 1200, 980, 0.653, 3000, 25),
                (GA, "Direct", 600, 500, 300, 0.500, 1200, 8),
            ],
            ("date", "country"): [
                (GA, "United Kingdom", 2000, 1600, 1300, 0.65, 4400, 33)
            ],
            ("date", "deviceCategory"): [
                (GA, "mobile", 1500, 1200, 900, 0.60, 3100, 20)
            ],
            ("date", "sessionSource"): [
                (GA, "google", 1500, 1200, 980, 0.65, 3000, 25)
            ],
            ("date", "sessionMedium"): [
                (GA, "organic", 1500, 1200, 980, 0.65, 3000, 25)
            ],
            ("date", "landingPagePlusQueryString"): [
                (GA, "/services", 500, 460, 380, 0.76, 700, 22)
            ],
            ("date", "eventName"): [
                (GA, "contact_form_submit", 22),
                (GA, "scroll", 1800),
            ],
        },
    )
    base.update(over)
    return FakeGoogle(**base)


async def _connected(client, user) -> str:
    created = await client.post(
        "/api/v1/websites",
        json={"url": "https://example.com"},
        headers=auth_headers(user),
    )
    website_id = created.json()["id"]
    start = await client.get(
        f"/api/v1/google/connect?website_id={website_id}"
        "&services=search_console&services=analytics",
        headers=auth_headers(user),
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    await client.get(f"/api/v1/google/callback?code=c&state={state}")

    props = await client.get(
        f"/api/v1/google/analytics/properties?website_id={website_id}",
        headers=auth_headers(user),
    )
    await client.post(
        f"/api/v1/google/analytics/properties/{props.json()[0]['id']}/connect",
        json={"website_id": website_id},
        headers=auth_headers(user),
    )
    return website_id


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_the_wizard_can_offer_the_properties_key_events(
    client, two_tenants, google
):
    """We list what GA4 has; we do not pick one."""
    user = two_tenants["user_a"]
    website_id = await _connected(client, user)

    response = await client.get(
        f"/api/v1/websites/{website_id}/analytics/events", headers=auth_headers(user)
    )
    events = {e["event_name"]: e for e in response.json()}
    assert set(events) == {"contact_form_submit", "purchase", "scroll"}
    # Telling a customer's own event apart from a GA4 built-in is useful.
    assert events["contact_form_submit"]["custom"] is True
    assert events["purchase"]["custom"] is False


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_outcomes_are_absent_until_the_customer_maps_an_event(
    client, two_tenants, google
):
    """Not zero. "We don't know" and "none happened" are different answers."""
    user = two_tenants["user_a"]
    website_id = await _connected(client, user)
    await client.post(
        f"/api/v1/websites/{website_id}/sync/analytics", headers=auth_headers(user)
    )

    body = (
        await client.get(
            f"/api/v1/websites/{website_id}/analytics", headers=auth_headers(user)
        )
    ).json()
    assert body["sessions"] == 2400
    assert body["outcomes_configured"] is False
    assert body["outcomes"] == []
    assert "can't work it out from the name" in body["outcomes_note"]


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_mapping_a_goal_makes_outcomes_appear(client, two_tenants, google):
    user = two_tenants["user_a"]
    website_id = await _connected(client, user)

    await client.post(
        f"/api/v1/websites/{website_id}/analytics/goals",
        json={
            "goals": [
                {
                    "event_name": "contact_form_submit",
                    "label": "Enquiry",
                    "goal_kind": "contact",
                    "is_primary": True,
                }
            ]
        },
        headers=auth_headers(user),
    )
    await client.post(
        f"/api/v1/websites/{website_id}/sync/analytics", headers=auth_headers(user)
    )

    body = (
        await client.get(
            f"/api/v1/websites/{website_id}/analytics", headers=auth_headers(user)
        )
    ).json()
    assert body["outcomes_configured"] is True
    assert body["outcomes"] == [
        {
            "event_name": "contact_form_submit",
            "label": "Enquiry",
            "goal_kind": "contact",
            "count": 22,
        }
    ]
    assert body["outcomes_note"] is None


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_only_mapped_events_are_stored(client, two_tenants, google, service_conn):
    """`scroll` is a key event in this property and means nothing to the
    business. Storing every key event fills the table with names nobody has
    given a meaning."""
    user = two_tenants["user_a"]
    website_id = await _connected(client, user)
    await client.post(
        f"/api/v1/websites/{website_id}/analytics/goals",
        json={"goals": [{"event_name": "contact_form_submit"}]},
        headers=auth_headers(user),
    )
    await client.post(
        f"/api/v1/websites/{website_id}/sync/analytics", headers=auth_headers(user)
    )

    rows = await (
        await service_conn.execute(
            "select distinct event_name from ga4_goal_daily where website_id = %s",
            (website_id,),
        )
    ).fetchall()
    assert [r["event_name"] for r in rows] == ["contact_form_submit"]


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_the_dimensional_tail_is_stored(client, two_tenants, google):
    user = two_tenants["user_a"]
    website_id = await _connected(client, user)
    await client.post(
        f"/api/v1/websites/{website_id}/sync/analytics", headers=auth_headers(user)
    )

    body = (
        await client.get(
            f"/api/v1/websites/{website_id}/analytics", headers=auth_headers(user)
        )
    ).json()
    assert [c["label"] for c in body["top_channels"]] == ["Organic Search", "Direct"]
    assert body["top_countries"][0]["label"] == "United Kingdom"
    assert body["top_devices"][0]["label"] == "mobile"


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_no_demographic_dimension_is_ever_requested(client, two_tenants, google):
    """GA4 thresholds data when demographics are present, silently withholding
    rows for small audiences — a quietly-wrong total, which is the one thing
    this product refuses to show."""
    user = two_tenants["user_a"]
    website_id = await _connected(client, user)
    await client.post(
        f"/api/v1/websites/{website_id}/sync/analytics", headers=auth_headers(user)
    )

    requested = {
        d["name"]
        for call in google.ga4_report_calls
        for d in call["dimensions"]
    }
    assert not requested & {
        "userAgeBracket", "userGender", "brandingInterest", "audienceName",
    }


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_replacing_the_goal_mapping_removes_the_old_one(
    client, two_tenants, google
):
    user = two_tenants["user_a"]
    website_id = await _connected(client, user)

    for events in (["contact_form_submit"], ["purchase"]):
        response = await client.post(
            f"/api/v1/websites/{website_id}/analytics/goals",
            json={"goals": [{"event_name": e} for e in events]},
            headers=auth_headers(user),
        )
    assert [g["event_name"] for g in response.json()] == ["purchase"]


async def test_syncing_analytics_without_a_connection_is_a_clear_error(
    client, two_tenants, google
):
    user = two_tenants["user_a"]
    created = await client.post(
        "/api/v1/websites",
        json={"url": f"https://na-{two_tenants['slug']}.example.com"},
        headers=auth_headers(user),
    )
    response = await client.post(
        f"/api/v1/websites/{created.json()['id']}/sync/analytics",
        headers=auth_headers(user),
    )
    assert response.status_code == 404
    assert "Analytics" in response.json()["error"]["message"]
