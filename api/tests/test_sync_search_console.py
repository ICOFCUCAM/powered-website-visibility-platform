"""Search Console synchronisation, end to end.

The acceptance criterion for this milestone (docs/09-mvp-sequence.md):

    the API returns daily clicks and impressions that RECONCILE with what the
    user sees in the Search Console UI, including the anonymised-clicks gap
    being displayed rather than hidden.

So the fake serves the shape Google actually serves: totals that include every
click, and query rows that omit the low-volume ones. If the code ever derives
totals by summing query rows, these tests fail.
"""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from api.tests.conftest import auth_headers
from api.tests.fake_google import FakeGoogle, site

# One day, chosen to sit inside BOTH the backfill window and the nightly
# incremental window, so the restatement test exercises a day the incremental
# run actually re-fetches. A fixed date would drift out of range as time
# passes and the test would quietly stop testing anything.
DAY = (date.today() - timedelta(days=4)).isoformat()

# Google reports 100 clicks in total, but the query dimension only accounts
# for 40 — the other 60 are withheld as anonymised low-volume queries.
TOTALS = [((DAY,), 100, 5000, 8.4)]
QUERIES = [
    ((DAY, "church in london"), 30, 1000, 3.0),
    ((DAY, "sunday worship london"), 10, 900, 12.0),
]
PAGES = [
    ((DAY, "https://example.com/"), 25, 2500, 6.0),
    ((DAY, "https://example.com/services"), 15, 1200, 9.5),
]


def _google(**overrides) -> FakeGoogle:
    return FakeGoogle(
        sites=[site("sc-domain:example.com")],
        search_analytics={
            ("date",): TOTALS,
            ("date", "query"): QUERIES,
            ("date", "page"): PAGES,
            ("date", "query", "page"): [
                ((DAY, "church in london", "https://example.com/"), 30, 1000, 3.0)
            ],
        },
        **overrides,
    )


async def _connected_website(client, user) -> str:
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
    return website_id


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_a_backfill_stores_totals_and_dimensions_separately(
    client, two_tenants, google, service_conn
):
    user = two_tenants["user_a"]
    website_id = await _connected_website(client, user)

    result = await client.post(
        f"/api/v1/websites/{website_id}/sync/search-console",
        headers=auth_headers(user),
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "succeeded"

    totals = await (
        await service_conn.execute(
            "select clicks, impressions from gsc_totals_daily "
            " where website_id = %s and date = %s",
            (website_id, DAY),
        )
    ).fetchone()
    assert (totals["clicks"], totals["impressions"]) == (100, 5000)

    summed = await (
        await service_conn.execute(
            "select coalesce(sum(clicks), 0) as clicks from gsc_query_daily "
            " where website_id = %s and date = %s",
            (website_id, DAY),
        )
    ).fetchone()
    # THE POINT: the query rows do not add up to the total, and the schema
    # stores both rather than pretending they do.
    assert summed["clicks"] == 40
    assert summed["clicks"] != totals["clicks"]


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_the_anonymised_gap_is_exposed_not_hidden(
    client, two_tenants, google, service_conn
):
    user = two_tenants["user_a"]
    website_id = await _connected_website(client, user)
    await client.post(
        f"/api/v1/websites/{website_id}/sync/search-console",
        headers=auth_headers(user),
    )

    row = await (
        await service_conn.execute(
            "select total_clicks, attributed_clicks, anonymised_clicks, "
            "       anonymised_share "
            "  from gsc_anonymised_share where website_id = %s and date = %s",
            (website_id, DAY),
        )
    ).fetchone()

    assert row["total_clicks"] == 100
    assert row["attributed_clicks"] == 40
    assert row["anonymised_clicks"] == 60
    assert float(row["anonymised_share"]) == pytest.approx(0.6)


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_each_dataset_is_fetched_with_its_own_dimensions(
    client, two_tenants, google
):
    """Four separate requests. The totals request carries no `query`
    dimension, which is the only reason it comes back complete."""
    user = two_tenants["user_a"]
    website_id = await _connected_website(client, user)
    await client.post(
        f"/api/v1/websites/{website_id}/sync/search-console",
        headers=auth_headers(user),
    )

    dimension_sets = {tuple(c["dimensions"]) for c in google.search_analytics_calls}
    assert ("date",) in dimension_sets
    assert ("date", "query") in dimension_sets
    assert ("date", "page") in dimension_sets

    for call in google.search_analytics_calls:
        # Never ask for provisional data: Google restates it and the chart
        # would move under the user with no explanation.
        assert call["dataState"] == "final"


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_the_second_run_is_incremental_not_another_backfill(
    client, two_tenants, google
):
    user = two_tenants["user_a"]
    website_id = await _connected_website(client, user)

    await client.post(
        f"/api/v1/websites/{website_id}/sync/search-console",
        headers=auth_headers(user),
    )
    backfill_calls = len(google.search_analytics_calls)

    google.search_analytics_calls.clear()
    second = await client.post(
        f"/api/v1/websites/{website_id}/sync/search-console",
        headers=auth_headers(user),
    )
    assert second.status_code == 200

    # A trailing five-day window, not sixteen months again.
    assert len(google.search_analytics_calls) < backfill_calls
    ranges = {(c["startDate"], c["endDate"]) for c in google.search_analytics_calls}
    assert len(ranges) == 1


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_re_syncing_restated_days_updates_rather_than_duplicates(
    client, two_tenants, google, service_conn
):
    """Google restates recent days. The second fetch must replace the first
    value, not collide with it or append a second row."""
    user = two_tenants["user_a"]
    website_id = await _connected_website(client, user)
    await client.post(
        f"/api/v1/websites/{website_id}/sync/search-console",
        headers=auth_headers(user),
    )

    # Google now reports more clicks for the same day.
    google.search_analytics[("date",)] = [((DAY, ), 140, 5200, 8.1)]
    await client.post(
        f"/api/v1/websites/{website_id}/sync/search-console",
        headers=auth_headers(user),
    )

    rows = await (
        await service_conn.execute(
            "select clicks from gsc_totals_daily where website_id = %s and date = %s",
            (website_id, DAY),
        )
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["clicks"] == 140


@pytest.mark.parametrize("google", [_google(quota_exceeded_after=2)], indirect=True)
async def test_hitting_quota_stops_cleanly_and_records_what_landed(
    client, two_tenants, google, service_conn
):
    """Quota is a reason to stop, not to fail. What landed is real, the run is
    recorded as partial with its range, and the next run resumes."""
    user = two_tenants["user_a"]
    website_id = await _connected_website(client, user)

    result = await client.post(
        f"/api/v1/websites/{website_id}/sync/search-console",
        headers=auth_headers(user),
    )
    body = result.json()
    assert body["status"] in ("partial", "failed")
    assert body["quota_hits"] == 1

    runs = await client.get(
        f"/api/v1/websites/{website_id}/sync-runs", headers=auth_headers(user)
    )
    run = runs.json()[0]
    assert run["quota_hits"] == 1
    assert run["range_start"] is not None and run["range_end"] is not None
    assert "quota" in (run["error"] or "")


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_sixteen_months_of_partitions_are_created_before_writing(
    client, two_tenants, google, service_conn
):
    """A missing partition is an INSERT failure, and the scheduler only ever
    looks forwards. The backfill provisions what it is about to write —
    through a SECURITY DEFINER helper, because the service role deliberately
    does not own the parent tables."""
    user = two_tenants["user_a"]
    website_id = await _connected_website(client, user)

    result = await client.post(
        f"/api/v1/websites/{website_id}/sync/search-console",
        headers=auth_headers(user),
    )
    assert result.json()["status"] == "succeeded"

    # The oldest month the backfill window reaches must now have a partition,
    # and it must be the right one for those dates.
    oldest = await (
        await service_conn.execute(
            """
            select to_char(min(date), 'YYYYMM') as month, count(*) as rows
              from gsc_query_daily where website_id = %s
            """,
            (website_id,),
        )
    ).fetchone()
    assert oldest["rows"] > 0

    covered = await (
        await service_conn.execute(
            "select count(*) as n from pg_class "
            " where relkind = 'r' and relname ~ '^gsc_query_daily_[0-9]{6}$'"
        )
    ).fetchone()
    assert covered["n"] >= 17


async def test_the_service_role_cannot_partition_arbitrary_tables(service_conn):
    """The SECURITY DEFINER helper is a narrow hole, not a general one."""
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await service_conn.execute(
            "select app.ensure_monthly_partition('organizations', '2024-01-01')"
        )


async def test_syncing_a_website_that_is_not_connected_is_a_clear_error(
    client, two_tenants, google
):
    user = two_tenants["user_a"]
    created = await client.post(
        "/api/v1/websites",
        json={"url": f"https://ns-{two_tenants['slug']}.example.com"},
        headers=auth_headers(user),
    )
    response = await client.post(
        f"/api/v1/websites/{created.json()['id']}/sync/search-console",
        headers=auth_headers(user),
    )
    assert response.status_code == 404
    assert "Search Console" in response.json()["error"]["message"]


@pytest.mark.parametrize("google", [_google()], indirect=True)
async def test_another_tenant_cannot_trigger_your_sync(client, two_tenants, google):
    website_id = await _connected_website(client, two_tenants["user_a"])
    stolen = await client.post(
        f"/api/v1/websites/{website_id}/sync/search-console",
        headers=auth_headers(two_tenants["user_b"]),
    )
    assert stolen.status_code == 404
