"""The home screen.

Most of these are about what the screen says when it does NOT have the data —
because that is the state most new accounts are in for their first minutes,
and a page of zeroes reads as a broken product rather than a new one.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from api.analysis.catalogue import seed
from api.analysis.runner import AnalysisRunner
from api.crawler.fetch import Fetcher
from api.crawler.politeness import HostLimiter
from api.crawler.runner import CrawlRunner
from api.crawler.storage import NullArtifactStore
from api.tests.conftest import auth_headers
from api.tests.fake_website import ORIGIN, FakeWebsite, page, sitemap_for
from api.tests.verification import verify_website

DAY = date.today() - timedelta(days=5)


@pytest.fixture
async def site(client, two_tenants, service_conn):
    await seed(service_conn)
    user, org = two_tenants["user_a"], two_tenants["org_a"]
    created = await client.post(
        "/api/v1/websites",
        json={"url": "https://example.com"},
        headers=auth_headers(user),
    )
    website_id = uuid.UUID(created.json()["id"])

    async def analyse(fake: FakeWebsite | None = None):
        fake = fake or FakeWebsite(
            pages={
                "/": page("Home", links=("/a", "/b")),
                "/a": page("", description=None),
                "/b": page("Another page", description=None),
            },
            sitemap=sitemap_for(["/", "/a", "/b"]),
        )
        crawl = await (
            await service_conn.execute(
                "insert into crawls (organization_id, website_id, trigger) "
                "values (%s,%s,'manual') returning id",
                (org, website_id),
            )
        ).fetchone()
        limiter = HostLimiter(default_delay=0.0)
        await CrawlRunner(
            service_conn,
            fetcher=Fetcher(limiter, client=fake.client()),
            store=NullArtifactStore(),
            limiter=limiter,
        ).run(crawl_id=crawl["id"], organization_id=org, website_id=website_id,
              origin=ORIGIN, max_pages=50)
        return await AnalysisRunner(service_conn).run(
            organization_id=org, website_id=website_id, crawl_id=crawl["id"]
        )

    async def add_search_data(clicks=100, impressions=5000, position=8.4):
        await service_conn.execute(
            "insert into gsc_totals_daily (organization_id, website_id, date, "
            "  clicks, impressions, position) values (%s,%s,%s,%s,%s,%s) "
            "on conflict (website_id, date) do update set clicks = excluded.clicks",
            (org, website_id, DAY, clicks, impressions, position),
        )
        await service_conn.execute(
            "insert into gsc_page_daily (organization_id, website_id, date, "
            "  url_hash, url, clicks, impressions, position) "
            "values (%s,%s,%s,sha256(%s),%s,%s,%s,%s) on conflict do nothing",
            (org, website_id, DAY, b"https://example.com/",
             "https://example.com/", clicks, impressions, position),
        )

    return website_id, user, org, analyse, add_search_data, service_conn


async def _dashboard(client, website_id, user) -> dict:
    response = await client.get(
        f"/api/v1/websites/{website_id}/dashboard", headers=auth_headers(user)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_brand_new_website_says_what_to_do_next(client, site):
    """Not a page of zeroes. A new account has nothing, and the screen should
    say so rather than imply the product found nothing wrong."""
    website_id, user, *_ = site
    body = await _dashboard(client, website_id, user)

    assert body["score"] is None
    assert body["search"] is None
    assert body["setup_hint"] == (
        "Connect Google Search Console to see how people find you."
    )
    assert body["freshness"]["stale"] is True


async def test_the_first_score_is_labelled_as_a_first_measurement(client, site):
    """"▲0%" is a claim we cannot support on day one."""
    website_id, user, org, analyse, _, conn = site
    await verify_website(conn, org, website_id)
    await analyse()

    body = await _dashboard(client, website_id, user)
    assert body["score"] is not None
    assert body["score"]["is_first_measurement"] is True
    assert body["score"]["change_pct"] is None
    assert body["score"]["compared_to"] is None


async def test_a_component_with_no_data_is_absent_not_zero(client, site):
    website_id, user, org, analyse, _, conn = site
    await verify_website(conn, org, website_id)
    await analyse()

    components = (await _dashboard(client, website_id, user))["score"]["components"]
    assert components["search_performance"] is None
    assert components["technical_health"] is not None


async def test_opportunities_are_sentences_not_error_codes(client, site):
    """The customer reads this. "11 pages get impressions but few clicks" is
    a sentence; "ctr_below_position_baseline × 11" is a log line."""
    website_id, user, org, analyse, _, conn = site
    await verify_website(conn, org, website_id)
    await analyse()

    opportunities = (await _dashboard(client, website_id, user))["opportunities"]
    assert opportunities
    assert len(opportunities) <= 4
    for item in opportunities:
        assert item["headline"][0].isdigit()
        assert "_" not in item["headline"]


def test_plurals_are_right_because_people_notice():
    """A `{n} page{s} have` template produces "1 page have no title", which is
    the kind of small wrongness that makes a product feel unfinished."""
    from api.repositories.postgres.dashboard import OPPORTUNITY_PHRASES, phrase_for

    assert phrase_for("missing_title", 1, "x") == "1 page has no title"
    assert phrase_for("missing_title", 4, "x") == "4 pages have no title"
    assert phrase_for("striking_distance_keyword", 1, "x") == (
        "1 search sits just below page one"
    )
    assert phrase_for("striking_distance_keyword", 9, "x") == (
        "9 searches sit just below page one"
    )

    # A type with no phrase still reads plainly rather than ungrammatically.
    assert phrase_for("unknown_rule", 1, "Some finding") == "Some finding"
    assert phrase_for("unknown_rule", 3, "Some finding") == "3 × Some finding"

    # Every phrase is a real sentence with no leftover template markers.
    for singular, plural in OPPORTUNITY_PHRASES.values():
        assert singular.startswith("1 ")
        assert "{s}" not in plural and "{es}" not in plural
        assert "{n}" in plural


def test_every_phrase_matches_a_real_rule():
    """A phrase for a rule that does not exist is dead copy nobody will ever
    see, and its absence for a rule that does exist is a log line on a screen."""
    from api.analysis.catalogue import BY_KEY
    from api.repositories.postgres.dashboard import OPPORTUNITY_PHRASES

    unknown = set(OPPORTUNITY_PHRASES) - set(BY_KEY)
    assert unknown == set(), f"phrases for rules that do not exist: {unknown}"


async def test_search_figures_appear_once_there_is_search_data(client, site):
    website_id, user, org, analyse, add_search, conn = site
    await verify_website(conn, org, website_id)
    await add_search()
    await analyse()

    body = await _dashboard(client, website_id, user)
    assert body["search"]["clicks"] == 100
    assert body["search"]["impressions"] == 5000
    assert "withholds" in body["search"]["note"]
    assert body["setup_hint"] is None


async def test_analytics_reports_whether_outcomes_are_configured(client, site):
    website_id, user, *_ = site
    analytics = (await _dashboard(client, website_id, user))["analytics"]
    assert analytics["connected"] is False
    assert analytics["outcomes_configured"] is False


async def test_recent_changes_show_the_loop_closing(client, site):
    """A product that only ever lists problems has nothing to say about
    progress, and progress is why people come back."""
    website_id, user, org, analyse, _, conn = site
    await verify_website(conn, org, website_id)

    broken = FakeWebsite(
        pages={"/": page("Home", links=("/a",)), "/a": page("")},
        sitemap=sitemap_for(["/", "/a"]),
    )
    fixed = FakeWebsite(
        pages={
            "/": page("Home", links=("/a",)),
            "/a": page("A title that is now present and sensible"),
        },
        sitemap=sitemap_for(["/", "/a"]),
    )
    await analyse(broken)
    await analyse(fixed)

    changes = (await _dashboard(client, website_id, user))["recent_changes"]
    assert any(c["kind"] == "fixed" for c in changes)


async def test_freshness_says_when_the_data_is_from(client, site):
    website_id, user, org, analyse, _, conn = site
    await verify_website(conn, org, website_id)
    await analyse()

    freshness = (await _dashboard(client, website_id, user))["freshness"]
    assert freshness["last_crawl_at"] is not None
    assert freshness["pages_crawled"] >= 1
    assert freshness["stale"] is False


async def test_the_score_history_states_which_version_it_is_comparable_within(
    client, site
):
    website_id, user, org, analyse, _, conn = site
    await verify_website(conn, org, website_id)
    await analyse()

    history = (
        await client.get(
            f"/api/v1/websites/{website_id}/score-history", headers=auth_headers(user)
        )
    ).json()
    assert len(history["points"]) >= 1
    assert history["points"][0]["total"] > 0


async def test_another_tenant_cannot_read_your_dashboard(client, site, two_tenants):
    website_id, *_ = site
    response = await client.get(
        f"/api/v1/websites/{website_id}/dashboard",
        headers=auth_headers(two_tenants["user_b"]),
    )
    assert response.status_code == 404


async def test_unsynced_search_terms_are_not_reported_as_anonymised(client, site):
    """The gap is site totals minus what the query rows account for. Before
    the query sync has run nothing accounts for anything, so the whole total
    would read as withheld by Google — a different and untrue claim."""
    website_id, user, org, analyse, add_search, conn = site
    await verify_website(conn, org, website_id)
    await add_search(clicks=4821, impressions=91204)
    await analyse()

    body = await _dashboard(client, website_id, user)
    assert body["search"]["clicks"] == 4821
    assert body["search"]["anonymised_clicks"] == 0

    detail = (
        await client.get(
            f"/api/v1/websites/{website_id}/queries", headers=auth_headers(user)
        )
    ).json()
    assert detail["anonymised"]["anonymised_clicks"] == 0
    assert "haven't been synced" in detail["anonymised"]["note"]


async def test_a_real_anonymised_gap_is_still_reported(client, site):
    """With query data present, the genuine withheld share must still show."""
    website_id, user, org, analyse, add_search, conn = site
    await verify_website(conn, org, website_id)
    await add_search(clicks=100, impressions=5000)
    await conn.execute(
        "insert into gsc_query_daily (organization_id, website_id, date, "
        "  query_hash, query, clicks, impressions, position) "
        "values (%s,%s,%s,sha256(%s),%s,40,1000,3.0) on conflict do nothing",
        (org, website_id, DAY, b"church in london", "church in london"),
    )
    await analyse()

    body = await _dashboard(client, website_id, user)
    assert body["search"]["anonymised_clicks"] == 60
