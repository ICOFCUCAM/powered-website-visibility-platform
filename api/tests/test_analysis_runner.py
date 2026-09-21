"""The analysis loop, end to end.

Crawl a site, analyse it, fix something, crawl again, and check the platform
says the right thing about what changed. That reconciliation is the product:
without it, every week produces a fresh list of near-duplicate advice and
nothing can ever be proved fixed.
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
from api.tests.fake_website import ORIGIN, FakeWebsite, page, sitemap_for
from api.tests.verification import verify_website

DAY = date.today() - timedelta(days=5)


@pytest.fixture
async def analysed(service_conn):
    """Crawl a site and analyse it, returning a callable to re-run."""
    await seed(service_conn)

    org, website_id = uuid.uuid4(), uuid.uuid4()
    slug = uuid.uuid4().hex[:8]
    await service_conn.execute(
        "insert into organizations (id,name,slug) values (%s,'A',%s)", (org, slug)
    )
    await service_conn.execute(
        "insert into websites (id, organization_id, domain, canonical_url) "
        "values (%s,%s,'example.com',%s)",
        (website_id, org, ORIGIN),
    )
    # Verified the way the product verifies: a covering, owner-held property.
    # Setting the timestamp alone would bypass the crawler's second gate.
    await verify_website(service_conn, org, website_id)

    async def run(site: FakeWebsite):
        crawl = await (
            await service_conn.execute(
                "insert into crawls (organization_id, website_id, trigger) "
                "values (%s,%s,'manual') returning id",
                (org, website_id),
            )
        ).fetchone()
        limiter = HostLimiter(default_delay=0.0)
        runner = CrawlRunner(
            service_conn,
            fetcher=Fetcher(limiter, client=site.client()),
            store=NullArtifactStore(),
            limiter=limiter,
        )
        await runner.run(
            crawl_id=crawl["id"], organization_id=org, website_id=website_id,
            origin=ORIGIN, max_pages=50,
        )
        return await AnalysisRunner(service_conn).run(
            organization_id=org, website_id=website_id, crawl_id=crawl["id"]
        )

    return run, org, website_id


async def _issues(conn, website_id, status=None):
    sql = "select type_key, status, severity, evidence from issues where website_id = %s"
    params = [website_id]
    if status:
        sql += " and status = %s"
        params.append(status)
    rows = await (await conn.execute(sql, params)).fetchall()
    return {r["type_key"]: r for r in rows}


async def test_a_page_with_no_title_produces_a_finding(analysed, service_conn):
    run, _, website_id = analysed
    site = FakeWebsite(
        pages={
            "/": page("Home", links=("/bad",)),
            "/bad": page("", description=None),
        },
        sitemap=sitemap_for(["/", "/bad"]),
    )
    result = await run(site)

    assert result.rules_run == 27
    issues = await _issues(service_conn, website_id)
    assert "missing_title" in issues
    assert "missing_meta_description" in issues


async def test_fixing_the_page_resolves_the_issue_rather_than_leaving_it(
    analysed, service_conn
):
    """The whole point of stable fingerprints."""
    run, _, website_id = analysed

    broken = FakeWebsite(
        pages={"/": page("Home", links=("/bad",)), "/bad": page("")},
        sitemap=sitemap_for(["/", "/bad"]),
    )
    await run(broken)
    before = await _issues(service_conn, website_id)
    assert before["missing_title"]["status"] == "open"

    fixed = FakeWebsite(
        pages={
            "/": page("Home", links=("/bad",)),
            "/bad": page("Now it has a proper title about counselling"),
        },
        sitemap=sitemap_for(["/", "/bad"]),
    )
    result = await run(fixed)

    after = await _issues(service_conn, website_id)
    assert after["missing_title"]["status"] == "resolved"
    assert result.issues_resolved >= 1


async def test_the_same_problem_is_one_issue_across_crawls_not_two(
    analysed, service_conn
):
    run, _, website_id = analysed
    site = FakeWebsite(
        pages={"/": page("Home", links=("/bad",)), "/bad": page("")},
        sitemap=sitemap_for(["/", "/bad"]),
    )
    await run(site)
    await run(site)

    rows = await (
        await service_conn.execute(
            "select count(*) as n from issues "
            " where website_id = %s and type_key = 'missing_title'",
            (website_id,),
        )
    ).fetchone()
    assert rows["n"] == 1


async def test_a_fix_that_comes_undone_is_reported_as_regressed(
    analysed, service_conn
):
    """"You fixed this and it came back" is a different message from "here is
    a problem", and the customer has earned the distinction."""
    run, _, website_id = analysed
    broken = FakeWebsite(
        pages={"/": page("Home", links=("/bad",)), "/bad": page("")},
        sitemap=sitemap_for(["/", "/bad"]),
    )
    fixed = FakeWebsite(
        pages={
            "/": page("Home", links=("/bad",)),
            "/bad": page("A perfectly reasonable page title here"),
        },
        sitemap=sitemap_for(["/", "/bad"]),
    )

    await run(broken)
    await run(fixed)
    result = await run(broken)

    issues = await _issues(service_conn, website_id)
    assert issues["missing_title"]["status"] == "regressed"
    assert result.issues_regressed >= 1


async def test_every_observation_is_appended_so_history_survives(
    analysed, service_conn
):
    run, _, website_id = analysed
    site = FakeWebsite(
        pages={"/": page("Home", links=("/bad",)), "/bad": page("")},
        sitemap=sitemap_for(["/", "/bad"]),
    )
    await run(site)
    await run(site)

    rows = await (
        await service_conn.execute(
            "select count(*) as n from issue_observations o "
            "  join issues i on i.id = o.issue_id "
            " where i.website_id = %s and i.type_key = 'missing_title'",
            (website_id,),
        )
    ).fetchone()
    assert rows["n"] == 2


async def test_a_dismissed_issue_stays_dismissed(analysed, service_conn):
    """The customer has already told us their answer. Re-raising it every week
    is how a recommendation list gets ignored."""
    run, _, website_id = analysed
    site = FakeWebsite(
        pages={"/": page("Home", links=("/bad",)), "/bad": page("")},
        sitemap=sitemap_for(["/", "/bad"]),
    )
    await run(site)
    await service_conn.execute(
        "update issues set status = 'dismissed' "
        " where website_id = %s and type_key = 'missing_title'",
        (website_id,),
    )
    await run(site)

    issues = await _issues(service_conn, website_id)
    assert issues["missing_title"]["status"] == "dismissed"


async def test_a_score_is_written_with_everything_that_produced_it(
    analysed, service_conn
):
    run, _, website_id = analysed
    site = FakeWebsite(
        pages={"/": page("Home", links=("/a",)), "/a": page("A page about things")},
        sitemap=sitemap_for(["/", "/a"]),
    )
    result = await run(site)

    assert result.score_total is not None
    row = await (
        await service_conn.execute(
            "select total, scoring_version, components, technical_health, "
            "       search_performance, deterministic "
            "  from score_snapshots where website_id = %s",
            (website_id,),
        )
    ).fetchone()

    assert row["scoring_version"] == "1.0.0"
    assert row["deterministic"] is True
    # Absent, not zero: this site has no Search Console data.
    assert row["search_performance"] is None
    # And every input is recorded so the chart point can be explained.
    assert "technical_health" in row["components"]
    assert "pages_evaluated" in row["components"]["technical_health"]


async def test_a_site_blocking_every_crawler_is_a_critical_finding(
    analysed, service_conn
):
    run, _, website_id = analysed
    site = FakeWebsite(
        pages={"/": page("Home")}, robots="User-agent: *\nDisallow: /\n"
    )
    await run(site)

    issues = await _issues(service_conn, website_id)
    assert issues["robots_blocks_crawl"]["severity"] == "critical"


async def test_blocked_ai_crawlers_become_a_finding(analysed, service_conn):
    run, _, website_id = analysed
    site = FakeWebsite(
        pages={"/": page("Home")},
        robots="User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n",
    )
    await run(site)

    issues = await _issues(service_conn, website_id)
    assert "ai_crawler_blocked" in issues
    assert "GPTBot" in issues["ai_crawler_blocked"]["evidence"]["blocked"]


async def test_duplicate_titles_are_found_across_pages(analysed, service_conn):
    run, _, website_id = analysed
    shared = "Our services"
    site = FakeWebsite(
        pages={
            "/": page("Home", links=("/a", "/b")),
            "/a": page(shared),
            "/b": page(shared),
        },
        sitemap=sitemap_for(["/", "/a", "/b"]),
    )
    await run(site)

    issues = await _issues(service_conn, website_id)
    assert "duplicate_title" in issues
    assert issues["duplicate_title"]["evidence"]["count"] == 2


async def test_one_broken_rule_does_not_lose_the_other_findings(
    analysed, service_conn, monkeypatch
):
    """Twenty-six working rules are worth more than a clean traceback."""
    from api.analysis.rules import base

    def exploding(ctx):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    original = base.all_rules
    monkeypatch.setattr(
        base, "all_rules",
        lambda: [("missing_title", exploding), *[
            r for r in original() if r[0] != "missing_title"
        ]],
    )
    monkeypatch.setattr("api.analysis.runner.all_rules", base.all_rules)

    run, _, website_id = analysed
    site = FakeWebsite(
        pages={
            "/": page("Home", links=("/bad",)),
            "/bad": page("", description=None),
        },
        sitemap=sitemap_for(["/", "/bad"]),
    )
    result = await run(site)

    issues = await _issues(service_conn, website_id)
    assert "missing_title" not in issues         # the broken rule found nothing
    assert "missing_meta_description" in issues  # the other rules still ran
    assert result.issues_open > 0
