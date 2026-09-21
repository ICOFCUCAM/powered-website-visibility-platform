"""Alerting, which is mostly a test of what it does NOT send.

Two failure modes, and they pull in opposite directions:

  NOISE. A nightly job that fails for four hundred websites must produce one
  alert, not four hundred, and must not produce that one again every fifteen
  minutes for a week. An operator who learns to ignore the channel has the
  same monitoring as one with no channel at all.

  SILENCE. An alert that fires once and then goes quiet while the problem
  continues is worse than one that repeats, because silence reads as
  recovery.

Most of what follows is about the line between them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from api.alerting import customer, operator
from api.alerting.channels import (
    Alert,
    DeliveryFailed,
    Fanout,
    NullNotifier,
    RecordingNotifier,
    WebhookNotifier,
    from_env,
)
from api.alerting.operator import Condition, reconcile, scan

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


@pytest.fixture
async def fleet(service_conn, two_tenants):
    """A clean slate: alerting reads across every tenant, so the other tests'
    leftovers would otherwise be this test's fleet."""
    await service_conn.execute("delete from operator_alerts")
    await service_conn.execute("delete from scheduled_runs")
    await service_conn.execute("delete from alert_events")
    await service_conn.execute("delete from crawls")
    await service_conn.execute("delete from connections")
    await service_conn.execute("update websites set archived_at = now()")

    org = two_tenants["org_a"]
    row = await (
        await service_conn.execute(
            "insert into websites (organization_id, domain, canonical_url) "
            "values (%s,%s,'https://example.com/') returning id",
            (org, f"{uuid.uuid4().hex[:8]}.example"),
        )
    ).fetchone()
    return org, row["id"], service_conn


async def add_runs(conn, org, website_id, job, *, failed, ok, error=None):
    for index in range(failed + ok):
        await conn.execute(
            "insert into scheduled_runs (organization_id, website_id, job,"
            " window_start, status, error, claimed_at)"
            " values (%s,%s,%s,%s,%s,%s,%s)",
            (
                org, website_id, job,
                NOW - timedelta(hours=6, minutes=index),
                "failed" if index < failed else "succeeded",
                error if index < failed else None,
                NOW - timedelta(hours=6),
            ),
        )


# -- what is worth an alert -------------------------------------------------
async def test_a_healthy_fleet_says_nothing(fleet):
    """The most important test in the file. A channel that fires on a healthy
    system is a channel people mute."""
    org, website_id, conn = fleet
    await add_runs(conn, org, website_id, "crawl_website", failed=0, ok=4)

    assert await scan(conn, now=NOW) == []


async def test_a_fresh_installation_does_not_page_anybody(service_conn):
    """Nothing has been dispatched because there is nothing to dispatch.
    Paging somebody about that on day one teaches them to ignore the channel
    before it has ever been useful."""
    await service_conn.execute("update websites set archived_at = now()")
    await service_conn.execute("delete from scheduled_runs")

    assert await scan(service_conn, now=NOW) == []


async def test_a_silent_scheduler_is_the_alert_that_catches_its_own_absence(fleet):
    """A scheduler that stopped makes every other alert silent too — no runs,
    no failures, nothing to report. This is the one condition that notices."""
    org, website_id, conn = fleet
    await add_runs(conn, org, website_id, "crawl_website", failed=0, ok=1)

    quiet = await scan(conn, now=NOW + timedelta(hours=12))
    kinds = {c.kind for c in quiet}

    assert "scheduler_quiet" in kinds
    alert = next(c for c in quiet if c.kind == "scheduler_quiet")
    assert alert.severity == "critical"
    assert "Every other alert is silent" in alert.body


async def test_four_hundred_failures_are_one_alert(fleet):
    """Aggregation before anything is sent. The alternative is four hundred
    messages at 01:07, which is the same as none."""
    org, website_id, conn = fleet
    await add_runs(conn, org, website_id, "sync_search_console", failed=400, ok=12)

    conditions = await scan(conn, now=NOW)
    failing = [c for c in conditions if c.kind == "job_failing"]

    assert len(failing) == 1
    assert "400 of 412" in failing[0].body
    assert failing[0].severity == "critical"


async def test_one_failure_is_a_warning_not_a_page(fleet):
    org, website_id, conn = fleet
    await add_runs(conn, org, website_id, "sync_analytics", failed=1, ok=40)

    condition = next(c for c in await scan(conn, now=NOW) if c.kind == "job_failing")
    assert condition.severity == "warning"


async def test_a_pool_that_is_down_says_so_rather_than_blaming_the_job(fleet):
    """"No worker picked this up" and "the job threw" send an operator to
    completely different places."""
    org, website_id, conn = fleet
    await add_runs(
        conn, org, website_id, "crawl_website",
        failed=6, ok=0, error="no worker picked this up",
    )

    condition = next(
        c for c in await scan(conn, now=NOW) if c.kind == "jobs_unclaimed"
    )
    assert condition.title == "No worker is running crawl_website"
    assert "pool for this job is down" in condition.body


async def test_one_customer_revoking_access_is_not_an_operator_alert(
    fleet, two_tenants
):
    """It is their business, and they get a notice of their own. Paging an
    operator about it is how the channel becomes noise."""
    org, website_id, conn = fleet
    await conn.execute(
        "insert into connections (organization_id, provider_key, external_id,"
        " label, status) values (%s,'google',%s,'a@example.com','needs_reauth')",
        (org, uuid.uuid4().hex),
    )
    for _ in range(9):
        await conn.execute(
            "insert into connections (organization_id, provider_key, external_id,"
            " label, status) values (%s,'google',%s,'b@example.com','active')",
            (org, uuid.uuid4().hex),
        )

    assert [c for c in await scan(conn, now=NOW)
            if c.kind == "connections_need_reauth"] == []


async def test_a_quarter_of_the_fleet_needing_reauth_is_ours(fleet):
    """That many at once is an OAuth client, a rotated secret or a scope
    change — and no amount of customer emails fixes it."""
    org, website_id, conn = fleet
    for index in range(10):
        await conn.execute(
            "insert into connections (organization_id, provider_key, external_id,"
            " label, status) values (%s,'google',%s,'a@example.com',%s)",
            (org, uuid.uuid4().hex, "needs_reauth" if index < 4 else "active"),
        )

    condition = next(
        c for c in await scan(conn, now=NOW) if c.kind == "connections_need_reauth"
    )
    assert condition.severity == "critical"
    assert "4 of 10" in condition.body


# -- the incident lifecycle -------------------------------------------------
def a_condition(kind="job_failing", subject="sync", severity="warning"):
    return Condition(
        kind=kind, subject=subject, severity=severity,
        title="Something is wrong", body="12 of 40 runs failed.",
    )


async def test_the_same_problem_is_one_incident_across_scans(fleet):
    org, website_id, conn = fleet
    notifier = RecordingNotifier()

    first = await reconcile(conn, [a_condition()], notifier, now=NOW)
    later = NOW + timedelta(minutes=15)
    second = await reconcile(conn, [a_condition()], notifier, now=later)

    assert first.opened == ["job_failing"]
    assert second.opened == [] and second.still_open == ["job_failing"]

    row = await (
        await conn.execute("select occurrences from operator_alerts")
    ).fetchone()
    assert row["occurrences"] == 2


async def test_a_persisting_problem_is_not_announced_every_scan(fleet):
    """Fifteen minutes apart, for a week, is how a channel gets muted."""
    org, website_id, conn = fleet
    notifier = RecordingNotifier()

    for minutes in (0, 15, 30, 45, 60):
        await reconcile(
            conn, [a_condition()], notifier, now=NOW + timedelta(minutes=minutes)
        )

    assert len(notifier.sent) == 1


async def test_but_it_is_mentioned_again_after_a_few_hours(fleet):
    """Silence reads as recovery. A problem that is still there deserves to
    be said again — and to say how long it has been going on."""
    org, website_id, conn = fleet
    notifier = RecordingNotifier()

    await reconcile(conn, [a_condition()], notifier, now=NOW)
    await reconcile(
        conn, [a_condition()], notifier,
        now=NOW + timedelta(hours=7), renotify_after=timedelta(hours=6),
    )

    assert len(notifier.sent) == 2
    assert "Seen 2 times since this started" in notifier.sent[1].body


async def test_a_problem_that_stops_is_resolved_and_said_so(fleet):
    org, website_id, conn = fleet
    notifier = RecordingNotifier()

    await reconcile(conn, [a_condition()], notifier, now=NOW)
    outcome = await reconcile(conn, [], notifier, now=NOW + timedelta(hours=1))

    assert outcome.resolved == ["job_failing"]
    assert notifier.sent[-1].severity == "recovered"
    assert notifier.sent[-1].title.startswith("Recovered:")
    assert "open for 1.0 hours" in notifier.sent[-1].body

    row = await (
        await conn.execute("select resolved_at from operator_alerts")
    ).fetchone()
    assert row["resolved_at"] is not None


async def test_resolving_something_nobody_was_told_about_is_not_news(fleet):
    """An incident that opened while no destination was configured has not
    been announced, so its recovery is not an announcement either."""
    org, website_id, conn = fleet

    await reconcile(conn, [a_condition()], NullNotifier(), now=NOW)
    notifier = RecordingNotifier()
    await reconcile(conn, [], notifier, now=NOW + timedelta(hours=1))

    assert notifier.sent == []


async def test_a_recovered_problem_that_returns_opens_a_new_incident(fleet):
    """The partial unique index is on OPEN incidents only, so history is kept
    and a recurrence is a new event rather than a resurrected one."""
    org, website_id, conn = fleet
    notifier = RecordingNotifier()

    await reconcile(conn, [a_condition()], notifier, now=NOW)
    await reconcile(conn, [], notifier, now=NOW + timedelta(hours=1))
    outcome = await reconcile(
        conn, [a_condition()], notifier, now=NOW + timedelta(hours=2)
    )

    assert outcome.opened == ["job_failing"]
    row = await (
        await conn.execute("select count(*) as n from operator_alerts")
    ).fetchone()
    assert row["n"] == 2


async def test_two_different_problems_are_two_incidents(fleet):
    org, website_id, conn = fleet
    notifier = RecordingNotifier()

    await reconcile(
        conn,
        [
            a_condition(subject="sync_search_console"),
            a_condition(subject="crawl_website"),
        ],
        notifier,
        now=NOW,
    )

    row = await (
        await conn.execute("select count(*) as n from operator_alerts")
    ).fetchone()
    assert row["n"] == 2


# -- delivery ---------------------------------------------------------------
async def test_an_undelivered_alert_is_not_recorded_as_sent(fleet):
    """Otherwise the incident goes quiet on the next scan having told
    nobody."""
    org, website_id, conn = fleet

    class Broken:
        configured = True

        async def send(self, alert):
            raise DeliveryFailed("the webhook returned 500")

    outcome = await reconcile(conn, [a_condition()], Broken(), now=NOW)

    assert outcome.undeliverable == 1
    row = await (
        await conn.execute("select notified_at, notify_count from operator_alerts")
    ).fetchone()
    assert row["notified_at"] is None and row["notify_count"] == 0


async def test_with_no_destination_configured_nothing_is_marked_told(fleet):
    """So the day somebody configures one, every open incident is announced
    rather than silently assumed handled."""
    org, website_id, conn = fleet

    await reconcile(conn, [a_condition()], NullNotifier(), now=NOW)

    row = await (
        await conn.execute("select notified_at from operator_alerts")
    ).fetchone()
    assert row["notified_at"] is None

    notifier = RecordingNotifier()
    await reconcile(conn, [a_condition()], notifier, now=NOW + timedelta(minutes=5))
    assert len(notifier.sent) == 1


async def test_one_dead_destination_does_not_silence_the_other(fleet):
    class Broken:
        configured = True

        async def send(self, alert):
            raise DeliveryFailed("down")

    working = RecordingNotifier()
    fanout = Fanout([Broken(), working])
    await fanout.send(Alert(severity="warning", title="t", body="b"))

    assert len(working.sent) == 1


async def test_a_webhook_carries_no_customer_domains(fleet, monkeypatch):
    """A chat channel is not a place for customer domains. An operator who
    needs the list queries scheduled_runs, where it is scoped and audited."""
    import httpx

    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        posted.append(_json.loads(request.content))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier("https://hooks.example/abc", client=client)

    org, website_id, conn = fleet
    await add_runs(conn, org, website_id, "sync_search_console", failed=5, ok=1)
    for condition in await scan(conn, now=NOW):
        await notifier.send(
            Alert(
                severity=condition.severity,
                title=condition.title,
                body=condition.body,
                detail=condition.detail,
            )
        )
    await client.aclose()

    domain = (
        await (
            await conn.execute("select domain from websites where id = %s", (website_id,))
        ).fetchone()
    )["domain"]
    assert posted, "nothing was posted"
    assert str(domain) not in str(posted)


async def test_a_refusing_webhook_is_an_error_not_a_silent_drop():
    import httpx

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    )
    notifier = WebhookNotifier("https://hooks.example/abc", client=client)
    with pytest.raises(DeliveryFailed):
        await notifier.send(Alert(severity="warning", title="t", body="b"))
    await client.aclose()


def test_with_nothing_configured_the_notifier_says_so(monkeypatch):
    """Not a logger pretending to be monitoring."""
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_EMAIL", raising=False)

    notifier = from_env()
    assert isinstance(notifier, NullNotifier)
    assert notifier.configured is False


def test_a_webhook_url_is_enough(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    monkeypatch.delenv("ALERT_EMAIL", raising=False)

    notifier = from_env()
    assert isinstance(notifier, WebhookNotifier)
    assert notifier.configured is True


# -- the customer's side ----------------------------------------------------
@pytest.fixture
async def linked(fleet):
    """A website with a Google connection that has gone bad."""
    org, website_id, conn = fleet
    connection = await (
        await conn.execute(
            "insert into connections (organization_id, provider_key, external_id,"
            " label, status) values (%s,'google',%s,'owner@example.com',"
            " 'needs_reauth') returning id",
            (org, uuid.uuid4().hex),
        )
    ).fetchone()
    prop = await (
        await conn.execute(
            "insert into connection_properties (organization_id, connection_id,"
            " provider_key, service, property_uri, permission_level)"
            " values (%s,%s,'google','search_console','sc-domain:example.com',"
            " 'siteOwner') returning id",
            (org, connection["id"]),
        )
    ).fetchone()
    await conn.execute(
        "insert into website_connections (organization_id, website_id,"
        " property_id, provider_key, service, status)"
        " values (%s,%s,%s,'google','search_console','active')",
        (org, website_id, prop["id"]),
    )
    return org, website_id, conn


class Mailbox:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, message):
        self.sent.append(message)


async def test_a_dead_connection_is_something_only_they_can_fix(linked):
    """It stops every figure on their dashboard updating, while the dashboard
    keeps showing yesterday's numbers — which looks like nothing is wrong."""
    org, website_id, conn = linked

    notices = await customer.find(conn)
    assert [n.kind for n in notices] == ["connection_needs_reauth:search_console"]
    assert notices[0].action.startswith("Open Settings")


async def test_a_customer_notice_is_sent_once(linked):
    org, website_id, conn = linked
    mailbox = Mailbox()

    first = await customer.run(conn, mailbox, now=NOW)
    second = await customer.run(conn, mailbox, now=NOW + timedelta(days=1))

    assert first.delivered == 1
    assert second.delivered == 0 and second.suppressed == 1
    assert len(mailbox.sent) == 1


async def test_it_is_said_again_once_enough_time_has_passed(linked):
    org, website_id, conn = linked
    mailbox = Mailbox()

    await customer.run(conn, mailbox, now=NOW)
    later = await customer.deliver(
        conn, await customer.find(conn), mailbox,
        now=NOW + timedelta(days=30), repeat_after=timedelta(days=14),
    )

    assert later.delivered == 1


async def test_a_notice_nobody_could_be_sent_is_not_recorded_as_delivered(linked):
    """"We told them" must never be inferred from the existence of the row."""
    org, website_id, conn = linked

    sent = await customer.run(conn, None, now=NOW)

    assert sent.undeliverable == 1 and sent.delivered == 0
    row = await (
        await conn.execute(
            "select delivered_at, delivery_error from alert_events"
            " where website_id = %s", (website_id,)
        )
    ).fetchone()
    assert row["delivered_at"] is None
    assert row["delivery_error"] == "no mail server configured"


async def test_the_email_names_the_website_and_what_to_do(linked):
    org, website_id, conn = linked
    mailbox = Mailbox()
    await customer.run(conn, mailbox, now=NOW, base_url="https://app.example")

    message = mailbox.sent[0]
    domain = (
        await (
            await conn.execute("select domain from websites where id = %s", (website_id,))
        ).fetchone()
    )["domain"]
    assert str(domain) in message.subject
    assert "What to do:" in message.text
    assert "https://app.example/settings" in message.text


async def test_a_customer_is_never_told_about_our_problems(fleet):
    """A failed sync because Google 500'd is ours. A customer who receives an
    alert they cannot act on learns to ignore the ones they can."""
    org, website_id, conn = fleet
    await add_runs(
        conn, org, website_id, "sync_search_console", failed=9, ok=0,
        error="GoogleError: 503",
    )

    assert await customer.find(conn) == []
    assert [c.kind for c in await scan(conn, now=NOW)] == ["job_failing"]


async def test_one_failed_crawl_is_a_blip_three_is_their_server(fleet):
    org, website_id, conn = fleet

    async def crawl(status):
        await conn.execute(
            "insert into crawls (organization_id, website_id, trigger, status,"
            " queued_at) values (%s,%s,'scheduled',%s, now())",
            (org, website_id, status),
        )

    await crawl("failed")
    assert [n for n in await customer.find(conn) if n.kind == "crawl_failing"] == []

    await crawl("failed")
    await crawl("failed")
    notices = [n for n in await customer.find(conn) if n.kind == "crawl_failing"]
    assert len(notices) == 1
    assert "Your Google figures are unaffected" in notices[0].body


async def test_a_recent_success_clears_the_crawl_notice(fleet):
    org, website_id, conn = fleet
    for status in ("failed", "failed", "failed"):
        await conn.execute(
            "insert into crawls (organization_id, website_id, trigger, status,"
            " queued_at) values (%s,%s,'scheduled',%s, now())",
            (org, website_id, status),
        )
    assert [n for n in await customer.find(conn) if n.kind == "crawl_failing"]

    await conn.execute(
        "insert into crawls (organization_id, website_id, trigger, status,"
        " queued_at) values (%s,%s,'scheduled','completed', now())",
        (org, website_id),
    )
    assert [n for n in await customer.find(conn) if n.kind == "crawl_failing"] == []


# -- the whole pass ---------------------------------------------------------
async def test_one_pass_looks_then_tells(fleet):
    org, website_id, conn = fleet
    await add_runs(conn, org, website_id, "calculate_scores", failed=4, ok=1)
    notifier = RecordingNotifier()

    outcome = await operator.run(conn, notifier, now=NOW)

    assert outcome.opened == ["job_failing"]
    assert outcome.notified == 1
    assert notifier.sent[0].severity == "critical"


async def test_a_problem_that_fixes_itself_immediately_reads_as_english(fleet):
    """"Open for 0 minutes" reads like a bug in the alerting."""
    org, website_id, conn = fleet
    notifier = RecordingNotifier()

    await reconcile(conn, [a_condition()], notifier, now=NOW)
    await reconcile(conn, [], notifier, now=NOW + timedelta(seconds=20))

    assert "less than a minute" in notifier.sent[-1].body


async def test_an_incident_is_clocked_by_the_scan_not_the_wall(fleet):
    """first_seen_at and last_seen_at are compared to each other and to the
    re-notify interval, so all three must come from one clock. Mixing the
    scan's clock with the database's makes a fresh incident look hours old."""
    org, website_id, conn = fleet

    await reconcile(conn, [a_condition()], RecordingNotifier(), now=NOW)

    row = await (
        await conn.execute(
            "select first_seen_at, last_seen_at, notified_at from operator_alerts"
        )
    ).fetchone()
    assert row["first_seen_at"] == NOW
    assert row["last_seen_at"] == NOW
    assert row["notified_at"] == NOW
