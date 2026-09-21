"""The nightly jobs, run for real.

Each one goes through the whole path a worker takes: claim the slot, open a
service connection, do the work with the same code the API uses, record the
outcome. The point is not that `CrawlRunner` works — that is tested next door
— but that the scheduler wires it up correctly and writes down what happened.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from api.analysis.catalogue import seed
from api.crawler.fetch import Fetcher
from api.crawler.politeness import HostLimiter
from api.crawler.storage import NullArtifactStore
from api.tests.fake_website import FakeWebsite, page, sitemap_for
from api.tests.verification import verify_website
from api.workers import jobs
from api.workers.dispatch import Candidate, claim, mark_running

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


@pytest.fixture
async def site(client, two_tenants, service_conn):
    """A verified, crawlable website. `client` is here for its lifespan: the
    jobs open their own service connections from the pool it starts."""
    await seed(service_conn)
    org = two_tenants["org_a"]
    row = await (
        await service_conn.execute(
            "insert into websites (organization_id, domain, canonical_url) "
            "values (%s,'example.com','https://example.com/') returning id",
            (org,),
        )
    ).fetchone()
    website_id = row["id"]
    await verify_website(service_conn, org, website_id)
    await service_conn.execute(
        "update websites set crawl_allowed = true where id = %s", (website_id,)
    )
    return org, website_id, service_conn


async def a_run(conn, org, website_id, job, *, window=NOW) -> int:
    site = Candidate(
        website_id=website_id, organization_id=org, domain="example.com",
        crawl_allowed=True, has_search_console=True, has_analytics=True,
        has_crawl=True,
    )
    run_id = await claim(conn, job=job, site=site, window_start=window)
    assert run_id is not None
    return run_id


async def run_row(conn, run_id):
    return await (
        await conn.execute(
            "select status, error, detail, started_at, finished_at"
            "  from scheduled_runs where id = %s",
            (run_id,),
        )
    ).fetchone()


def fake_site() -> FakeWebsite:
    return FakeWebsite(
        pages={
            "/": page("Home", links=("/a", "/b")),
            "/a": page("", description=None),
            "/b": page("Another page", description=None),
        },
        sitemap=sitemap_for(["/", "/a", "/b"]),
    )


# -- the crawl --------------------------------------------------------------
async def test_the_crawl_job_crawls_and_writes_down_what_it_found(site):
    org, website_id, conn = site
    run_id = await a_run(conn, org, website_id, "crawl_website")
    limiter = HostLimiter(default_delay=0.0)

    result = await jobs.crawl_website(
        run_id,
        store=NullArtifactStore(),
        fetcher=Fetcher(limiter, client=fake_site().client()),
        limiter=limiter,
    )

    assert result["status"] == "succeeded"
    assert result["pages_fetched"] >= 3

    row = await run_row(conn, run_id)
    assert row["status"] == "succeeded"
    assert row["detail"]["pages_fetched"] >= 3
    assert row["started_at"] and row["finished_at"]

    crawl = await (
        await conn.execute(
            "select trigger, status from crawls where website_id = %s"
            " order by queued_at desc limit 1",
            (website_id,),
        )
    ).fetchone()
    assert (crawl["trigger"], crawl["status"]) == ("scheduled", "completed")


async def test_a_run_already_taken_does_no_work(site):
    """A broker redelivery must not crawl the site a second time."""
    org, website_id, conn = site
    run_id = await a_run(conn, org, website_id, "crawl_website")
    await mark_running(conn, run_id)  # somebody else got there first

    result = await jobs.crawl_website(run_id, store=NullArtifactStore())

    assert result == {"status": "skipped"}
    crawls = await (
        await conn.execute(
            "select count(*) as n from crawls where website_id = %s", (website_id,)
        )
    ).fetchone()
    assert crawls["n"] == 0


async def test_a_website_deleted_between_claim_and_run_is_skipped(site):
    org, website_id, conn = site
    run_id = await a_run(conn, org, website_id, "calculate_scores")
    await conn.execute(
        "update websites set archived_at = now() where id = %s", (website_id,)
    )

    assert (await jobs.calculate_scores(run_id))["status"] == "skipped"
    assert (await run_row(conn, run_id))["error"] == "website is gone"


# -- analysis, plan, report -------------------------------------------------
@pytest.fixture
async def crawled(site):
    org, website_id, conn = site
    run_id = await a_run(conn, org, website_id, "crawl_website")
    limiter = HostLimiter(default_delay=0.0)
    await jobs.crawl_website(
        run_id,
        store=NullArtifactStore(),
        fetcher=Fetcher(limiter, client=fake_site().client()),
        limiter=limiter,
    )
    return org, website_id, conn


async def test_the_scoring_job_analyses_the_latest_crawl(crawled):
    """Nightly rather than only after a crawl: half the rules read Search
    Console, so a site whose pages have not changed can still acquire a
    finding overnight."""
    org, website_id, conn = crawled
    run_id = await a_run(conn, org, website_id, "calculate_scores")

    result = await jobs.calculate_scores(run_id)
    assert result["status"] == "succeeded"
    assert result["issues_open"] >= 1
    assert result["score"] is not None

    snapshot = await (
        await conn.execute(
            "select total from score_snapshots where website_id = %s", (website_id,)
        )
    ).fetchone()
    assert snapshot is not None


async def test_the_recommendations_job_writes_this_week_s_plan(crawled):
    org, website_id, conn = crawled
    await jobs.calculate_scores(await a_run(conn, org, website_id, "calculate_scores"))

    run_id = await a_run(conn, org, website_id, "generate_recommendations")
    result = await jobs.generate_recommendations(run_id)

    assert result["status"] == "succeeded"
    assert result["priorities"] >= 1
    # No API key in the test environment, so the prose is templated — and the
    # record says which, rather than leaving a reader to guess.
    assert result["fallback_reason"] == "no_provider"

    plan = await (
        await conn.execute(
            "select week_start from plans where website_id = %s", (website_id,)
        )
    ).fetchone()
    assert plan["week_start"] == date.today() - timedelta(
        days=date.today().weekday()
    )


async def test_the_report_job_generates_but_does_not_claim_to_have_sent(
    crawled, monkeypatch
):
    """A generated draft nobody receives is a reasonable state. A row marked
    sent when no mail server exists is not."""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    org, website_id, conn = crawled
    await jobs.calculate_scores(await a_run(conn, org, website_id, "calculate_scores"))

    run_id = await a_run(conn, org, website_id, "generate_weekly_report")
    result = await jobs.generate_weekly_report(run_id)

    assert result["status"] == "succeeded"
    assert result["sent"] is False

    report = await (
        await conn.execute(
            "select status, sent_at from reports where website_id = %s", (website_id,)
        )
    ).fetchone()
    assert (report["status"], report["sent_at"]) == ("draft", None)


# -- sync -------------------------------------------------------------------
async def test_a_sync_with_no_link_is_skipped_with_a_reason(site):
    """Not a failure. A nightly failed row for a website that simply has not
    connected Analytics would bury the real failures."""
    org, website_id, conn = site
    run_id = await a_run(conn, org, website_id, "sync_analytics")

    result = await jobs.sync_analytics(run_id)

    assert result["status"] == "skipped"
    assert result["reason"] == "no active analytics link"
    assert (await run_row(conn, run_id))["status"] == "skipped"


# -- failure ----------------------------------------------------------------
async def test_a_failing_job_records_why_and_re_raises(site, monkeypatch):
    """Re-raised so Celery can retry it; recorded so "did last night run?"
    has an answer that is not a log search."""
    org, website_id, conn = site
    run_id = await a_run(conn, org, website_id, "calculate_scores")

    async def explode(*args, **kwargs):
        raise RuntimeError("the analysis fell over")

    monkeypatch.setattr("api.analysis.runner.AnalysisRunner.run", explode)

    with pytest.raises(RuntimeError):
        await jobs.calculate_scores(run_id)

    row = await run_row(conn, run_id)
    assert row["status"] == "failed"
    assert "the analysis fell over" in row["error"]
    assert row["finished_at"] is not None


# -- housekeeping -----------------------------------------------------------
async def test_partitions_are_kept_ahead_of_the_fact_tables(client, service_conn):
    """A missing partition is an INSERT failure, not a silent drop."""
    await jobs.ensure_partitions(months=3)

    ahead = date.today().replace(day=1) + timedelta(days=62)
    rows = await (
        await service_conn.execute(
            """
            select count(*) as n from pg_class c
              join pg_inherits i on i.inhrelid = c.oid
              join pg_class p on p.oid = i.inhparent
             where p.relname = 'gsc_query_daily' and c.relname like %s
            """,
            (f"gsc_query_daily_{ahead:%Y%m}",),
        )
    ).fetchone()
    assert rows["n"] == 1, f"no partition for {ahead:%Y-%m}"


async def test_an_unknown_job_name_is_not_silently_dispatchable():
    assert set(jobs.BY_NAME) == {
        "sync_search_console", "sync_analytics", "crawl_website",
        "calculate_scores", "generate_recommendations", "generate_weekly_report",
    }
    assert "drop_everything" not in jobs.BY_NAME
