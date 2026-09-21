"""The crawler, end to end against a fake website.

Real robots parsing, real sitemap reading, real extraction, real frontier,
real database. Only the network is replaced.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from api.crawler.fetch import Fetcher
from api.crawler.politeness import HostLimiter
from api.crawler.runner import CrawlRunner
from api.crawler.storage import NullArtifactStore
from api.tests.fake_website import ORIGIN, FakeWebsite, page, sitemap_for
from api.tests.verification import verify_website


@pytest.fixture
def website_rows(service_conn):
    """An organisation and website to crawl into.

    Verified by default, because the crawler refuses an unverified website —
    that is the second gate of decision 20, and a fixture that quietly
    bypassed it would make every other test here prove less than it looks.
    """

    async def _make(verified: bool = True):
        org, site_id, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        slug = uuid.uuid4().hex[:8]
        await service_conn.execute(
            "insert into organizations (id,name,slug) values (%s,'C',%s)", (org, slug)
        )
        await service_conn.execute(
            "insert into users (id,email) values (%s,%s)", (user, f"{slug}@example.com")
        )
        await service_conn.execute(
            "insert into websites (id, organization_id, domain, canonical_url) "
            "values (%s,%s,%s,%s)",
            (site_id, org, "example.com", ORIGIN),
        )
        if verified:
            await verify_website(service_conn, org, site_id)
        crawl = await (
            await service_conn.execute(
                "insert into crawls (organization_id, website_id, trigger) "
                "values (%s,%s,'manual') returning id",
                (org, site_id),
            )
        ).fetchone()
        return org, site_id, crawl["id"]

    return _make


async def _crawl(service_conn, website_rows, site: FakeWebsite, *, max_pages=100):
    org, site_id, crawl_id = await website_rows()
    limiter = HostLimiter(default_delay=0.0)
    runner = CrawlRunner(
        service_conn,
        fetcher=Fetcher(limiter, client=site.client()),
        store=NullArtifactStore(),
        limiter=limiter,
    )
    summary = await runner.run(
        crawl_id=crawl_id,
        organization_id=org,
        website_id=site_id,
        origin=ORIGIN,
        max_pages=max_pages,
    )
    return summary, site_id, crawl_id


async def test_a_whole_small_site_is_crawled_from_its_sitemap(
    service_conn, website_rows
):
    site = FakeWebsite(
        pages={
            "/": page("Home", links=("/about", "/services")),
            "/about": page("About"),
            "/services": page("Services", links=("/services/counselling",)),
            "/services/counselling": page("Counselling"),
        },
        sitemap=sitemap_for(["/", "/about", "/services"]),
        robots="User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n",
    )
    summary, site_id, crawl_id = await _crawl(service_conn, website_rows, site)

    assert summary.fetched == 4
    assert summary.errors == 0

    rows = await (
        await service_conn.execute(
            "select p.url, s.title, s.word_count from page_snapshots s "
            "  join pages p on p.id = s.page_id where s.crawl_id = %s order by p.url",
            (crawl_id,),
        )
    ).fetchall()
    assert [r["url"] for r in rows] == [
        f"{ORIGIN}/",
        f"{ORIGIN}/about",
        f"{ORIGIN}/services",
        f"{ORIGIN}/services/counselling",
    ]
    assert rows[0]["title"] == "Home"
    assert all(r["word_count"] > 0 for r in rows)


async def test_pages_linked_but_not_in_the_sitemap_are_still_found(
    service_conn, website_rows
):
    site = FakeWebsite(
        pages={"/": page("Home", links=("/hidden",)), "/hidden": page("Hidden")},
        sitemap=sitemap_for(["/"]),
    )
    summary, _, crawl_id = await _crawl(service_conn, website_rows, site)
    assert summary.fetched == 2


async def test_robots_disallow_is_obeyed(service_conn, website_rows):
    site = FakeWebsite(
        pages={"/": page("Home", links=("/admin/secret", "/ok")),
               "/admin/secret": page("Secret"), "/ok": page("OK")},
        robots="User-agent: *\nDisallow: /admin\n",
    )
    summary, _, crawl_id = await _crawl(service_conn, website_rows, site)

    assert summary.skipped >= 1
    fetched = {r for r in site.requests if "/admin" in r}
    assert fetched == set(), "a disallowed path was fetched"


async def test_a_site_blocking_everything_is_a_finding_not_a_failure(
    service_conn, website_rows
):
    site = FakeWebsite(
        pages={"/": page("Home")}, robots="User-agent: *\nDisallow: /\n"
    )
    summary, _, crawl_id = await _crawl(service_conn, website_rows, site)

    assert summary.robots_blocked is True
    assert summary.stopped_reason == "robots_disallows_crawl"
    assert summary.fetched == 0

    row = await (
        await service_conn.execute(
            "select status, error_summary from crawls where id = %s", (crawl_id,)
        )
    ).fetchone()
    # Completed, not failed: the crawler did its job and found something.
    assert row["status"] == "completed"
    assert row["error_summary"]["robots_blocked"] is True


async def test_the_crawler_identifies_itself(service_conn, website_rows):
    site = FakeWebsite(pages={"/": page("Home")})
    await _crawl(service_conn, website_rows, site)
    assert all("VisibilityBot" in ua for ua in site.user_agents)
    assert any("+https://" in ua for ua in site.user_agents)


async def test_the_page_cap_stops_the_crawl_and_is_recorded(
    service_conn, website_rows
):
    """A truncated crawl that looks complete produces analysis that is quietly
    wrong, so hitting the cap is surfaced."""
    site = FakeWebsite(
        pages={f"/p{i}": page(f"Page {i}", links=tuple(f"/p{j}" for j in range(12)))
               for i in range(12)}
        | {"/": page("Home", links=tuple(f"/p{i}" for i in range(12)))},
    )
    summary, _, crawl_id = await _crawl(service_conn, website_rows, site, max_pages=5)

    assert summary.fetched == 5
    assert summary.hit_page_cap is True
    assert summary.stopped_reason == "page_cap_reached"

    # The frontier honestly shows what was not reached.
    pending = await (
        await service_conn.execute(
            "select count(*) as n from crawl_frontier "
            " where crawl_id = %s and state = 'pending'",
            (crawl_id,),
        )
    ).fetchone()
    assert pending["n"] > 0


async def test_fetch_failures_are_classified_not_just_counted(
    service_conn, website_rows
):
    """"17 errors" is not actionable. "17 DNS failures" is."""
    site = FakeWebsite(
        pages={"/": page("Home", links=("/gone", "/broken"))},
        statuses={"/gone": 404, "/broken": 500},
    )
    summary, _, crawl_id = await _crawl(service_conn, website_rows, site)

    # An HTTP error status is a successful fetch with a bad status, not a
    # transport failure — it is a finding about the site.
    rows = await (
        await service_conn.execute(
            "select s.status_code from page_snapshots s where s.crawl_id = %s "
            " order by s.status_code",
            (crawl_id,),
        )
    ).fetchall()
    assert {r["status_code"] for r in rows} == {200, 404, 500}


async def test_external_links_are_recorded_but_not_followed(
    service_conn, website_rows
):
    site = FakeWebsite(
        pages={"/": page("Home", links=("https://other.test/x", "/about")),
               "/about": page("About")},
    )
    summary, site_id, crawl_id = await _crawl(service_conn, website_rows, site)

    assert summary.fetched == 2
    assert not any("other.test" in r for r in site.requests)

    external = await (
        await service_conn.execute(
            "select count(*) as n from page_links "
            " where crawl_id = %s and is_internal = false",
            (crawl_id,),
        )
    ).fetchone()
    assert external["n"] == 1


async def test_the_same_page_linked_many_ways_is_fetched_once(
    service_conn, website_rows
):
    site = FakeWebsite(
        pages={
            "/": page("Home", links=(
                "/about", "/about?utm_source=x", "/about#team", "//example.com/about",
            )),
            "/about": page("About"),
        },
    )
    summary, _, _ = await _crawl(service_conn, website_rows, site)
    about_requests = [r for r in site.requests if r.rstrip("/").endswith("/about")]
    assert len(about_requests) == 1
    assert summary.fetched == 2


async def test_raw_html_goes_to_object_storage_not_postgres(
    service_conn, website_rows, tmp_path: Path
):
    from api.crawler.storage import LocalArtifactStore

    site = FakeWebsite(pages={"/": page("Home")})
    org, site_id, crawl_id = await website_rows()
    store = LocalArtifactStore(tmp_path)
    limiter = HostLimiter(default_delay=0.0)
    runner = CrawlRunner(
        service_conn,
        fetcher=Fetcher(limiter, client=site.client()),
        store=store,
        limiter=limiter,
    )
    await runner.run(
        crawl_id=crawl_id, organization_id=org, website_id=site_id,
        origin=ORIGIN, max_pages=10,
    )

    row = await (
        await service_conn.execute(
            "select raw_key from page_snapshots where crawl_id = %s limit 1",
            (crawl_id,),
        )
    ).fetchone()
    assert row["raw_key"] is not None
    stored = await store.get(row["raw_key"])
    assert stored is not None and b"<title>Home</title>" in stored


async def test_ai_crawler_blocks_are_reported_as_a_finding(
    service_conn, website_rows
):
    site = FakeWebsite(
        pages={"/": page("Home")},
        robots="User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n",
    )
    summary, _, _ = await _crawl(service_conn, website_rows, site)
    assert "GPTBot" in summary.ai_crawlers_blocked


async def test_the_crawler_refuses_a_website_it_may_no_longer_crawl(
    service_conn, website_rows
):
    """The second gate (decision 20).

    Admission checked permission when the job was queued. Verification can be
    revoked between then and the worker picking it up, and a queue entry must
    never outlive the permission that created it.
    """
    site = FakeWebsite(pages={"/": page("Home")})
    org, site_id, crawl_id = await website_rows(verified=False)

    limiter = HostLimiter(default_delay=0.0)
    runner = CrawlRunner(
        service_conn,
        fetcher=Fetcher(limiter, client=site.client()),
        store=NullArtifactStore(),
        limiter=limiter,
    )
    summary = await runner.run(
        crawl_id=crawl_id, organization_id=org, website_id=site_id,
        origin=ORIGIN, max_pages=10,
    )

    assert summary.stopped_reason == "ownership_not_verified"
    assert summary.fetched == 0
    assert site.requests == [], "a page was fetched without permission"

    row = await (
        await service_conn.execute(
            "select status from crawls where id = %s", (crawl_id,)
        )
    ).fetchone()
    assert row["status"] == "cancelled"


async def test_a_verified_website_is_crawled(service_conn, website_rows):
    """The same code path, with permission in place."""
    site = FakeWebsite(pages={"/": page("Home")})
    org, site_id, crawl_id = await website_rows()

    limiter = HostLimiter(default_delay=0.0)
    runner = CrawlRunner(
        service_conn,
        fetcher=Fetcher(limiter, client=site.client()),
        store=NullArtifactStore(),
        limiter=limiter,
    )
    summary = await runner.run(
        crawl_id=crawl_id, organization_id=org, website_id=site_id,
        origin=ORIGIN, max_pages=10,
    )
    assert summary.stopped_reason is None
    assert summary.fetched == 1
