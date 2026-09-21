"""The weekly report, end to end.

The milestone's acceptance criterion is here: *the figures in the email match
the dashboard exactly for the same window*. It is checked by rendering both
from the same seeded website and comparing, because that is the only version
of the check that would have caught the two ways this actually goes wrong —
a different window, and a different rounding.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import date, timedelta

import pytest

from api.analysis.catalogue import seed
from api.analysis.runner import AnalysisRunner
from api.crawler.fetch import Fetcher
from api.crawler.politeness import HostLimiter
from api.crawler.runner import CrawlRunner
from api.crawler.storage import NullArtifactStore
from api.reports import links, runner
from api.reports.mail import RecordingMailer
from api.tests.conftest import auth_headers
from api.tests.fake_website import ORIGIN, FakeWebsite, page, sitemap_for
from api.tests.verification import verify_website

DAY = date.today() - timedelta(days=5)


@pytest.fixture
async def site(client, two_tenants, service_conn):
    await seed(service_conn)
    await service_conn.execute("delete from issue_explanations")
    user, org = two_tenants["user_a"], two_tenants["org_a"]
    created = await client.post(
        "/api/v1/websites",
        json={"url": "https://example.com"},
        headers=auth_headers(user),
    )
    website_id = uuid.UUID(created.json()["id"])
    await verify_website(service_conn, org, website_id)

    fake = FakeWebsite(
        pages={
            "/": page("Home", links=("/a", "/b")),
            "/a": page("", description=None),
            "/b": page("", description=None),
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

    await service_conn.execute(
        "insert into gsc_totals_daily (organization_id, website_id, date, clicks,"
        "  impressions, position) values (%s,%s,%s,%s,%s,%s) "
        "on conflict (website_id, date) do update set clicks = excluded.clicks",
        (org, website_id, DAY, 1234, 48219, 8.42),
    )
    await AnalysisRunner(service_conn).run(
        organization_id=org, website_id=website_id, crawl_id=crawl["id"]
    )
    return website_id, user, org, service_conn


async def make_report(client, website_id, user) -> dict:
    response = await client.post(
        f"/api/v1/websites/{website_id}/reports", headers=auth_headers(user)
    )
    assert response.status_code == 201, response.text
    return response.json()


# -- generating -------------------------------------------------------------
async def test_a_report_is_generated_with_a_plan_behind_it(client, site):
    website_id, user, _, conn = site
    body = await make_report(client, website_id, user)

    assert body["status"] == "draft"
    assert body["subject"].startswith("example.com: 1,234 clicks")
    row = await (
        await conn.execute(
            "select status, plan_id, payload from reports where id = %s",
            (uuid.UUID(body["id"]),),
        )
    ).fetchone()
    assert str(row["plan_id"]) == body["plan_id"]
    assert row["payload"]["search"]["clicks"] == 1234


async def test_without_a_provider_the_report_is_templated_and_says_so(client, site):
    """Nothing about the figures changes. The prose is ours instead of a
    model's, which is a normal outcome the record should name rather than
    hide."""
    website_id, user, *_ = site
    body = await make_report(client, website_id, user)
    assert body["fallback_reason"] == "no_provider"


async def test_regenerating_replaces_the_draft_rather_than_stacking(client, site):
    website_id, user, _, conn = site
    first = await make_report(client, website_id, user)
    second = await make_report(client, website_id, user)

    assert first["id"] == second["id"]
    count = await (
        await conn.execute(
            "select count(*) as n from reports where website_id = %s", (website_id,)
        )
    ).fetchone()
    assert count["n"] == 1


async def test_a_sent_report_is_not_quietly_rewritten(client, site):
    """What went out is what is on record."""
    website_id, user, org, conn = site
    first = await make_report(client, website_id, user)
    await conn.execute(
        "update reports set status = 'sent', sent_at = now() where id = %s",
        (uuid.UUID(first["id"]),),
    )

    result = await runner.generate(
        conn, organization_id=org, website_id=website_id
    )
    assert result.status == "sent"


# -- the acceptance criterion ----------------------------------------------
async def test_the_email_figures_match_the_dashboard(client, site):
    """The milestone's own test. Same window, same repositories, same
    rounding — checked against the rendered HTML, not just the payload, so a
    formatting difference counts as a difference."""
    website_id, user, *_ = site

    dashboard = (
        await client.get(
            f"/api/v1/websites/{website_id}/dashboard", headers=auth_headers(user)
        )
    ).json()
    body = await make_report(client, website_id, user)
    html = (await client.get(f"/api/v1{body['html_url'][len('/api/v1'):]}")).text

    search = dashboard["search"]
    assert f"{search['clicks']:,}" in html
    assert f"{search['impressions']:,}" in html
    assert f"{search['ctr'] * 100:.2f}%" in html
    assert f"{search['position']:.1f}" in html
    assert f"{dashboard['score']['total']:.0f}/100" in html


async def test_the_plain_text_part_carries_the_same_figures(client, site):
    """A client that strips the HTML is also the one most likely to be the
    only thing some customers read."""
    website_id, _, org, conn = site
    result = await runner.generate(
        conn, organization_id=org, website_id=website_id
    )
    assert "1,234" in result.text
    assert "48,219" in result.text
    assert result.text.count("1,234") >= 1


async def test_the_email_never_quotes_a_number_the_data_does_not_hold(client, site):
    """The templated prose is held to rule 1 as well: every figure in the
    rendered email must come from the report's own payload."""
    from api.ai.numbers import unsupported_figures

    website_id, _, org, conn = site
    result = await runner.generate(
        conn, organization_id=org, website_id=website_id
    )
    prose = [result.subject, result.figures.summary] + [
        part
        for priority in result.figures.priorities
        for part in [priority.title, priority.why, *priority.how]
    ]
    assert unsupported_figures(prose, result.figures.as_payload()) == []


# -- the signed link --------------------------------------------------------
async def test_the_html_link_works_without_a_session(client, site):
    """It is read in a mail client, where there is no session and no header."""
    website_id, user, *_ = site
    body = await make_report(client, website_id, user)

    response = await client.get(f"/api/v1{body['html_url'][len('/api/v1'):]}")
    assert response.status_code == 200
    assert "example.com" in response.text
    assert response.headers["content-type"].startswith("text/html")


async def test_a_tampered_signature_is_refused(client, site):
    website_id, user, *_ = site
    body = await make_report(client, website_id, user)
    url = re.sub(r"signature=\w+", "signature=" + "0" * 64, body["html_url"])

    assert (await client.get(url)).status_code == 404


async def test_changing_the_report_id_does_not_reuse_the_signature(client, site):
    """The id is what was signed, so a valid link cannot be pointed at
    somebody else's report."""
    website_id, user, *_ = site
    body = await make_report(client, website_id, user)
    url = body["html_url"].replace(body["id"], str(uuid.uuid4()))

    assert (await client.get(url)).status_code == 404


def test_an_expired_link_is_refused():
    report_id, secret = uuid.uuid4(), "a-secret-at-least-32-bytes-long-here"
    expired = int(time.time()) - 60
    assert not links.verify(
        report_id,
        secret,
        expires_at=expired,
        signature=links.sign(report_id, secret, expires_at=expired),
    )


# -- tenancy ----------------------------------------------------------------
async def test_another_organisation_cannot_generate_or_list_reports(client, site):
    website_id, _, _, _ = site
    intruder = auth_headers(uuid.uuid4(), "intruder@example.com")

    assert (
        await client.post(
            f"/api/v1/websites/{website_id}/reports", headers=intruder
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/websites/{website_id}/reports", headers=intruder
        )
    ).status_code == 404


# -- sending ----------------------------------------------------------------
async def test_sending_without_a_mail_server_is_an_error_not_a_silent_success(
    client, site, monkeypatch
):
    """Marking a report 'sent' when nothing was sent leaves a customer waiting
    for an email that was never going to arrive."""
    website_id, user, *_ = site
    body = await make_report(client, website_id, user)
    monkeypatch.delenv("SMTP_HOST", raising=False)

    response = await client.post(
        f"/api/v1/websites/{website_id}/reports/{body['id']}/send",
        headers=auth_headers(user),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "email_not_configured"


async def test_a_delivered_report_records_its_recipients(client, site):
    website_id, user, org, conn = site
    body = await make_report(client, website_id, user)
    mailer = RecordingMailer()
    recipients = await runner.recipients_for(conn, org)

    await runner.send(
        conn,
        report_id=uuid.UUID(body["id"]),
        mailer=mailer,
        recipients=recipients,
    )

    row = await (
        await conn.execute(
            "select status, sent_at, recipients from reports where id = %s",
            (uuid.UUID(body["id"]),),
        )
    ).fetchone()
    assert row["status"] == "sent"
    assert row["recipients"] == recipients
    assert mailer.sent[0].subject == body["subject"]
    assert "<html" in mailer.sent[0].html
    assert mailer.sent[0].text


async def test_a_failed_delivery_is_recorded_as_failed(client, site):
    """A report that did not arrive must look different from one that did."""
    website_id, user, org, conn = site
    body = await make_report(client, website_id, user)

    class BrokenMailer:
        async def send(self, message):
            raise OSError("connection refused")

    with pytest.raises(OSError):
        await runner.send(
            conn,
            report_id=uuid.UUID(body["id"]),
            mailer=BrokenMailer(),
            recipients=["a@example.com"],
        )

    row = await (
        await conn.execute(
            "select status, error, sent_at from reports where id = %s",
            (uuid.UUID(body["id"]),),
        )
    ).fetchone()
    assert row["status"] == "failed"
    assert "connection refused" in row["error"]
    assert row["sent_at"] is None


# -- the plan endpoints -----------------------------------------------------
async def test_the_plan_is_readable_with_its_recommendations(client, site):
    website_id, user, *_ = site
    await make_report(client, website_id, user)

    plan = (
        await client.get(
            f"/api/v1/websites/{website_id}/plan", headers=auth_headers(user)
        )
    ).json()

    assert plan["recommendations"], "a website with findings should have a plan"
    first = plan["recommendations"][0]
    assert first["rank"] == 1
    assert first["prose_source"] == "template"
    assert first["pages_affected"] >= 1


async def test_a_website_with_no_plan_yet_is_not_an_error(client, site):
    """An ordinary state. The screen should say "we haven't written one" and
    not show an error page."""
    website_id, user, *_ = site
    plan = (
        await client.get(
            f"/api/v1/websites/{website_id}/plan", headers=auth_headers(user)
        )
    ).json()
    assert plan["id"] is None and plan["recommendations"] == []


async def test_marking_a_recommendation_done_is_intent_not_evidence(client, site):
    """Saying you fixed it does not resolve the issue — only the next crawl
    observing it gone does. The plan can then say "you marked this done and it
    is still there", which is one of the more useful sentences we have."""
    website_id, user, _, conn = site
    await make_report(client, website_id, user)
    plan = (
        await client.get(
            f"/api/v1/websites/{website_id}/plan", headers=auth_headers(user)
        )
    ).json()
    recommendation = plan["recommendations"][0]

    response = await client.post(
        f"/api/v1/websites/{website_id}/recommendations/{recommendation['id']}"
        "/complete",
        headers=auth_headers(user),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "RESOLVED"

    issue = await (
        await conn.execute(
            "select status from issues where website_id = %s and status = 'open' "
            "limit 1",
            (website_id,),
        )
    ).fetchone()
    assert issue is not None, "the issue itself stays open until a crawl says otherwise"


async def test_an_unknown_status_filter_is_rejected_rather_than_ignored(client, site):
    """Returning an empty list for a filter we do not understand looks exactly
    like "you have none", which is a different and wrong answer."""
    website_id, user, *_ = site
    response = await client.get(
        f"/api/v1/websites/{website_id}/recommendations?status=done",
        headers=auth_headers(user),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_status"


# -- what the rendering must never do --------------------------------------
def _figures(**overrides):
    from api.reports.weekly import PriorityView, ReportFigures

    defaults = dict(
        website={"domain": "example.com", "name": "Example"},
        period_start=date(2026, 8, 22),
        period_end=date(2026, 9, 18),
        week_start=date(2026, 9, 21),
        score=None,
        search=None,
        summary="Nothing much moved.",
        priorities=[],
        movements=[],
    )
    defaults.update(overrides)
    return ReportFigures(**defaults), PriorityView


def test_a_priority_with_nothing_to_show_does_not_say_and_one_more():
    """A keyword finding names a search, not a page, and a finding derived
    from Search Console can name a URL we never crawled. Either way, "and 1
    more" under an empty list tells the reader nothing and looks like a bug,
    because it is."""
    from api.reports import render

    figures, PriorityView = _figures()
    figures.priorities = [
        PriorityView(
            rank=1, title="Fix something", why="", how=["Do it."], count=1,
            estimated_clicks_delta=0, effort="low", examples=[],
            prose_source="template",
        )
    ]
    assert "more" not in render.text(figures, dashboard_url=None).split("Fix")[1]
    assert "and 1 more" not in render.html(figures)


def test_a_search_phrase_is_named_rather_than_counted():
    from api.reports import render

    figures, PriorityView = _figures()
    figures.priorities = [
        PriorityView(
            rank=1, title="Push a search onto page one", why="", how=["Do it."],
            count=1, estimated_clicks_delta=0, effort="medium",
            examples=["sourdough bread near me"], prose_source="template",
        )
    ]
    assert "sourdough bread near me" in render.text(figures)
    assert "sourdough bread near me" in render.html(figures)


def test_an_improving_position_is_never_shown_as_a_plus():
    """Position is the one metric where the sign lies: a reader seeing "+5.6%"
    beside "12.4" reads it as the number 12.4 going up, which is the opposite
    of what happened."""
    from api.reports import render

    figures, _ = _figures(
        search={
            "clicks": 1456, "impressions": 50400, "ctr": 0.0289, "position": 12.4,
            "change": {"clicks": 18.2, "impressions": 5.3, "position": 5.6},
        }
    )
    text = render.text(figures)
    assert "5.6% better than the previous 28 days" in text
    assert "+5.6%" not in text
    # Clicks keep the sign, because there the sign means what it says.
    assert "+18.2%" in text


def test_a_first_score_says_so_rather_than_standing_alone():
    from api.reports import render

    figures, _ = _figures(
        score={"total": 52.0, "as_of": "2026-09-18", "change_pct": None,
               "compared_to": None}
    )
    assert "first measurement" in render.text(figures)
    assert "first measurement" in render.html(figures)


def test_the_figures_are_escaped_into_the_html():
    """Page titles and URLs come from somebody else's website."""
    from api.reports import render

    figures, PriorityView = _figures(
        website={"domain": "<script>alert(1)</script>", "name": ""}
    )
    figures.priorities = [
        PriorityView(
            rank=1, title="<img onerror=alert(1)>", why="", how=["<b>x</b>"],
            count=1, estimated_clicks_delta=0, effort="low",
            examples=["https://x.test/<svg/onload=alert(1)>"],
            prose_source="template",
        )
    ]
    html = render.html(figures)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "onerror=alert(1)>" not in html


async def test_generating_a_report_from_the_request_path_can_meter_its_spend(
    client, site
):
    """The bug this test exists for.

    `llm_calls` carries a read policy and no write policy, so the RLS-bound
    request role cannot insert into it. Everything that metered until now ran
    as the service role in a background job, and "generate my report now" is
    the first metered call on the request path — with no model configured it
    never metered at all, so nothing failed and nothing noticed.
    """
    from api.ai import deps as ai_deps
    from api.tests.fake_model import FakeProvider

    website_id, user, org, conn = site
    ai_deps.set_provider(
        FakeProvider(
            responses=lambda payload: {
                "summary": "Clicks are up.",
                "priorities": [
                    {
                        "ref": finding["ref"],
                        "title": finding["suggested_title"],
                        "why": "Worth doing.",
                        "how": ["Do it."],
                    }
                    for finding in payload["findings"]
                ],
            }
        )
    )
    try:
        body = await make_report(client, website_id, user)
    finally:
        ai_deps.set_provider(None)

    assert body["fallback_reason"] is None
    row = await (
        await conn.execute(
            "select purpose, status, cost_usd from llm_calls where website_id = %s",
            (website_id,),
        )
    ).fetchone()
    assert (row["purpose"], row["status"]) == ("weekly_plan", "ok")
