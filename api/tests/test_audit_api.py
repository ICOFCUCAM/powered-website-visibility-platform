"""The audit screen's API."""

from __future__ import annotations

import uuid

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


@pytest.fixture
async def audited(client, two_tenants, service_conn):
    """A crawled and analysed website belonging to org A."""
    await seed(service_conn)
    user, org = two_tenants["user_a"], two_tenants["org_a"]

    created = await client.post(
        "/api/v1/websites",
        json={"url": "https://example.com"},
        headers=auth_headers(user),
    )
    website_id = uuid.UUID(created.json()["id"])
    await verify_website(service_conn, org, website_id)

    site = FakeWebsite(
        pages={
            "/": page("Home", links=("/a", "/b")),
            "/a": page("", description=None),
            "/b": page("Shared title", description=None),
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
        fetcher=Fetcher(limiter, client=site.client()),
        store=NullArtifactStore(),
        limiter=limiter,
    ).run(
        crawl_id=crawl["id"], organization_id=org, website_id=website_id,
        origin=ORIGIN, max_pages=50,
    )
    await AnalysisRunner(service_conn).run(
        organization_id=org, website_id=website_id, crawl_id=crawl["id"]
    )
    return str(website_id), user


async def test_the_audit_reports_checks_run_and_what_was_found(client, audited):
    website_id, user = audited
    body = (
        await client.get(f"/api/v1/websites/{website_id}/audit",
                         headers=auth_headers(user))
    ).json()

    assert body["checks_run"] == 27
    assert body["issues_open"] > 0
    assert sum(body["counts"].values()) == body["issues_open"]
    assert set(body["by_category"]) <= {"technical", "content", "seo", "ai_search"}


async def test_findings_are_ranked_by_estimated_clicks_then_severity(
    client, audited
):
    website_id, user = audited
    issues = (
        await client.get(f"/api/v1/websites/{website_id}/audit",
                         headers=auth_headers(user))
    ).json()["issues"]

    impacts = [i["impact_score"] for i in issues]
    assert impacts == sorted(impacts, reverse=True)


async def test_the_audit_says_when_search_data_is_missing(client, audited):
    """Several rules simply cannot run without Search Console. Saying so beats
    implying a clean bill of health."""
    website_id, user = audited
    body = (
        await client.get(f"/api/v1/websites/{website_id}/audit",
                         headers=auth_headers(user))
    ).json()
    assert body["search_data_available"] is False


async def test_an_issue_carries_its_history(client, audited):
    website_id, user = audited
    listing = await client.get(
        f"/api/v1/websites/{website_id}/audit", headers=auth_headers(user)
    )
    issue_id = listing.json()["issues"][0]["id"]

    detail = (
        await client.get(f"/api/v1/websites/{website_id}/audit/{issue_id}",
                         headers=auth_headers(user))
    ).json()
    assert detail["rule_version"] == "1.0.0"
    assert len(detail["history"]) >= 1
    assert detail["history"][0]["present"] is True
    assert detail["title"] and detail["summary"]


async def test_marking_an_issue_fixed_starts_verification_it_does_not_end_it(
    client, audited, service_conn
):
    """The customer's word begins the check. A later crawl finishes it."""
    website_id, user = audited
    listing = await client.get(
        f"/api/v1/websites/{website_id}/audit", headers=auth_headers(user)
    )
    issue_id = listing.json()["issues"][0]["id"]

    resolved = await client.post(
        f"/api/v1/websites/{website_id}/audit/{issue_id}/resolve",
        json={"note": "changed the title"},
        headers=auth_headers(user),
    )
    assert resolved.status_code == 200
    # Applied, not resolved: nothing has verified it yet.
    assert resolved.json()["status"] == "applied"

    action = await (
        await service_conn.execute(
            "select capability, status, before_state, applied_by, notes "
            "  from actions where issue_id = %s",
            (uuid.UUID(issue_id),),
        )
    ).fetchone()
    assert action["capability"] == "manual.mark_fixed"
    # The revertibility constraint applies to manual actions too.
    assert action["before_state"] is not None
    assert action["notes"] == "changed the title"


async def test_filters_narrow_the_list(client, audited):
    website_id, user = audited
    content_only = (
        await client.get(
            f"/api/v1/websites/{website_id}/audit?category=content",
            headers=auth_headers(user),
        )
    ).json()
    assert all(i["category"] == "content" for i in content_only["issues"])
    assert content_only["issues"]


async def test_another_tenant_cannot_read_your_audit(client, audited, two_tenants):
    website_id, _ = audited
    response = await client.get(
        f"/api/v1/websites/{website_id}/audit",
        headers=auth_headers(two_tenants["user_b"]),
    )
    assert response.status_code == 404
