"""Claiming the night's work.

Two failure modes, and both of them are silent:

  A schedule that RUNS TWICE crawls a customer's site twice, syncs Google
  twice, and bills them twice for the AI.

  A schedule that SILENTLY DOES NOT RUN leaves a customer looking at Monday's
  numbers on Friday, and nothing anywhere says so.

The unique index on (website_id, job, window_start) is the answer to the
first. Claiming the most recent slot that has PASSED — rather than reacting to
a cron tick — is the answer to the second.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from api.adapters import db
from api.workers.dispatch import claim, dispatch, finish, mark_running, reap
from api.workers.schedule import BY_NAME, JOBS, due_slot

WEDNESDAY = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


@pytest.fixture
async def site(service_conn, two_tenants):
    """A website that is eligible for everything: verified, crawlable, linked
    to both Google services, and with one completed crawl behind it."""
    org = two_tenants["org_a"]
    website = await (
        await service_conn.execute(
            "insert into websites (organization_id, domain, canonical_url) "
            "values (%s,%s,%s) returning id",
            (org, f"{uuid.uuid4().hex[:8]}.example", "https://example.com/"),
        )
    ).fetchone()
    website_id = website["id"]

    connection = await (
        await service_conn.execute(
            "insert into connections (organization_id, provider_key, external_id,"
            " label, status) values (%s,'google',%s,'owner@example.com','active')"
            " returning id",
            (org, uuid.uuid4().hex),
        )
    ).fetchone()
    for service in ("search_console", "analytics"):
        prop = await (
            await service_conn.execute(
                "insert into connection_properties (organization_id, connection_id,"
                " provider_key, service, property_uri, permission_level)"
                " values (%s,%s,'google',%s,%s,'siteOwner') returning id",
                (org, connection["id"], service, f"{service}://example.com"),
            )
        ).fetchone()
        await service_conn.execute(
            "insert into website_connections (organization_id, website_id,"
            " property_id, provider_key, service, status)"
            " values (%s,%s,%s,'google',%s,'active')",
            (org, website_id, prop["id"], service),
        )

    await service_conn.execute(
        "insert into crawls (organization_id, website_id, trigger, status,"
        " finished_at) values (%s,%s,'manual','completed', now())",
        (org, website_id),
    )
    # crawl_allowed is derived and revoked from client roles, so the service
    # role sets it the way the ownership pipeline would.
    await service_conn.execute(
        "update websites set crawl_allowed = true, ownership_verified_at = now() "
        " where id = %s",
        (website_id,),
    )
    return org, website_id, service_conn


def collector():
    sent: list = []
    return sent, lambda entry: sent.append(entry)


async def tick(conn, now=WEDNESDAY, **kwargs):
    sent, enqueue = collector()
    claimed = await dispatch(conn, now=now, enqueue=enqueue, **kwargs)
    return claimed, sent


async def runs_for(conn, website_id):
    rows = await (
        await conn.execute(
            "select job, status, window_start, trigger from scheduled_runs"
            " where website_id = %s order by job",
            (website_id,),
        )
    ).fetchall()
    return rows


# -- claiming ---------------------------------------------------------------
async def test_a_full_night_is_claimed_once(site):
    org, website_id, conn = site
    claimed, sent = await tick(conn)

    mine = [entry for entry in claimed if entry.website_id == website_id]
    assert {entry.job for entry in mine} == {job.name for job in JOBS}
    assert len(sent) == len(claimed), "everything claimed must reach the queue"


async def test_a_second_tick_in_the_same_slot_claims_nothing(site):
    """The unique index is the lock. Two dispatchers racing on the same tick
    produce one winner and one no-op — no advisory lock, no leader election,
    no Redis key that can expire mid-crawl."""
    org, website_id, conn = site
    first, _ = await tick(conn)
    second, sent = await tick(conn)

    assert [e for e in first if e.website_id == website_id]
    assert [e for e in second if e.website_id == website_id] == []
    assert sent == []


async def test_the_next_day_s_slot_is_a_new_claim(site):
    org, website_id, conn = site
    await tick(conn)
    tomorrow, _ = await tick(conn, now=WEDNESDAY + timedelta(days=1))

    assert {e.job for e in tomorrow if e.website_id == website_id} == {
        job.name for job in JOBS if job.weekday is None
    }


async def test_an_overnight_outage_runs_last_night_late(site):
    """The property the whole design is for: a missed window is late, not
    lost. Nothing ran at 01:00; the tick at 09:00 claims that same slot."""
    org, website_id, conn = site
    claimed, _ = await tick(conn, now=WEDNESDAY)

    sync = next(e for e in claimed if e.job == "sync_search_console"
                and e.website_id == website_id)
    expected = due_slot(BY_NAME["sync_search_console"], website_id, WEDNESDAY)
    assert sync.window_start == expected
    assert sync.window_start.hour == 1
    assert sync.window_start < WEDNESDAY


async def test_a_week_of_outage_is_still_one_night_of_work(site):
    org, website_id, conn = site
    claimed, _ = await tick(conn, now=WEDNESDAY + timedelta(days=7))

    per_job = [e.job for e in claimed if e.website_id == website_id]
    assert len(per_job) == len(set(per_job)), "a backlog, not one slot per job"


async def test_each_claim_names_the_pool_its_job_belongs_to(site):
    org, website_id, conn = site
    claimed, _ = await tick(conn)
    queues = {e.job: e.queue for e in claimed if e.website_id == website_id}
    assert queues["crawl_website"] == "crawl"
    assert queues["sync_search_console"] == "sync"
    assert queues["generate_weekly_report"] == "reports"


# -- eligibility ------------------------------------------------------------
async def test_a_website_with_no_analytics_link_is_skipped_not_failed(site):
    """Nothing is wrong with a customer who has not connected Analytics. A
    nightly failed row would say there was, and bury the real ones."""
    org, website_id, conn = site
    await conn.execute(
        "update website_connections set status = 'unlinked'"
        " where website_id = %s and service = 'analytics'",
        (website_id,),
    )
    claimed, _ = await tick(conn)

    jobs = {e.job for e in claimed if e.website_id == website_id}
    assert "sync_analytics" not in jobs
    assert "sync_search_console" in jobs


async def test_a_website_that_may_not_be_crawled_is_not_queued_for_one(site):
    org, website_id, conn = site
    await conn.execute(
        "update websites set crawl_allowed = false where id = %s", (website_id,)
    )
    claimed, _ = await tick(conn)
    assert "crawl_website" not in {
        e.job for e in claimed if e.website_id == website_id
    }


async def test_nothing_reports_on_a_website_that_has_never_been_crawled(site):
    """Run before the first crawl lands, the score would be a fiction and the
    weekly report would describe a website nobody has looked at."""
    org, website_id, conn = site
    await conn.execute(
        "update crawls set status = 'failed' where website_id = %s", (website_id,)
    )
    claimed, _ = await tick(conn)

    jobs = {e.job for e in claimed if e.website_id == website_id}
    assert jobs == {"sync_search_console", "sync_analytics", "crawl_website"}


async def test_an_archived_website_drops_out_of_the_schedule(site):
    org, website_id, conn = site
    await conn.execute(
        "update websites set archived_at = now() where id = %s", (website_id,)
    )
    claimed, _ = await tick(conn)
    assert [e for e in claimed if e.website_id == website_id] == []


# -- the run's lifecycle ----------------------------------------------------
async def test_a_run_refuses_a_second_start(site):
    """A broker redelivery, or a retry after a worker was killed mid-crawl,
    must not start the work again."""
    org, website_id, conn = site
    claimed, _ = await tick(conn)
    run = next(e for e in claimed if e.website_id == website_id)

    assert await mark_running(conn, run.run_id) is not None
    assert await mark_running(conn, run.run_id) is None


async def test_a_finished_run_records_what_it_did(site):
    org, website_id, conn = site
    claimed, _ = await tick(conn)
    run = next(e for e in claimed if e.job == "crawl_website"
               and e.website_id == website_id)

    await mark_running(conn, run.run_id)
    await finish(conn, run.run_id, status="succeeded", detail={"pages_fetched": 42})

    row = await (
        await conn.execute(
            "select status, detail, finished_at from scheduled_runs where id = %s",
            (run.run_id,),
        )
    ).fetchone()
    assert row["status"] == "succeeded"
    assert row["detail"]["pages_fetched"] == 42
    assert row["finished_at"] is not None


async def test_a_failure_keeps_its_reason(site):
    org, website_id, conn = site
    claimed, _ = await tick(conn)
    run = claimed[0]
    await mark_running(conn, run.run_id)
    await finish(conn, run.run_id, status="failed", error="Google said 429")

    row = await (
        await conn.execute(
            "select status, error from scheduled_runs where id = %s", (run.run_id,)
        )
    ).fetchone()
    assert (row["status"], row["error"]) == ("failed", "Google said 429")


async def test_a_manual_claim_is_marked_as_one(site):
    """A customer pressing "scan now" and the nightly schedule both land in
    the same table, and an operator should be able to tell them apart."""
    from api.workers.dispatch import candidates

    org, website_id, conn = site
    site_row = next(c for c in await candidates(conn) if c.website_id == website_id)
    run_id = await claim(
        conn, job="crawl_website", site=site_row,
        window_start=WEDNESDAY, trigger="manual",
    )
    assert run_id is not None
    rows = await runs_for(conn, website_id)
    assert rows[0]["trigger"] == "manual"


# -- tenancy ----------------------------------------------------------------
async def test_the_schedule_log_is_readable_only_by_its_own_organisation(
    client, site
):
    """An operational log is still tenant data. A customer may see that
    Tuesday's sync failed; nobody else may."""
    org, website_id, conn = site
    await tick(conn)

    async with db.session() as unbound:
        rows = await (
            await unbound.execute(
                "select count(*) as n from scheduled_runs where website_id = %s",
                (website_id,),
            )
        ).fetchone()
    assert rows["n"] == 0, "an unbound session read another tenant's schedule"


async def test_a_brand_new_website_does_not_wait_for_tomorrow(service_conn, two_tenants):
    """The spec's "new accounts run the same jobs immediately through the
    queue rather than waiting for the next window" — and it needs no special
    path to get it. Claiming the most recent slot that has PASSED means a
    website connected at lunchtime has this morning's slot behind it already,
    so its first sync starts on the next tick.
    """
    org = two_tenants["org_b"]
    row = await (
        await service_conn.execute(
            "insert into websites (organization_id, domain, canonical_url) "
            "values (%s,%s,'https://new.example/') returning id",
            (org, f"{uuid.uuid4().hex[:8]}.example"),
        )
    ).fetchone()
    connection = await (
        await service_conn.execute(
            "insert into connections (organization_id, provider_key, external_id,"
            " label, status) values (%s,'google',%s,'owner@example.com','active')"
            " returning id",
            (org, uuid.uuid4().hex),
        )
    ).fetchone()
    prop = await (
        await service_conn.execute(
            "insert into connection_properties (organization_id, connection_id,"
            " provider_key, service, property_uri, permission_level)"
            " values (%s,%s,'google','search_console','sc-domain:new.example',"
            " 'siteOwner') returning id",
            (org, connection["id"]),
        )
    ).fetchone()
    await service_conn.execute(
        "insert into website_connections (organization_id, website_id, property_id,"
        " provider_key, service, status) values (%s,%s,%s,'google',"
        " 'search_console','active')",
        (org, row["id"], prop["id"]),
    )

    lunchtime = datetime(2026, 9, 23, 12, 30, tzinfo=UTC)
    claimed, _ = await tick(service_conn, now=lunchtime)
    mine = {e.job for e in claimed if e.website_id == row["id"]}

    assert "sync_search_console" in mine
    # And not the jobs that would describe a website nobody has crawled yet.
    assert "calculate_scores" not in mine
    assert "generate_weekly_report" not in mine


async def test_a_run_whose_worker_died_becomes_a_visible_failure(site):
    """The crawl frontier's lease, applied to the schedule: a slot whose
    worker died must become visible WITHOUT that worker coming back to admit
    it. A row left `running` forever is worse than a failure, because a
    failure is something an operator can see."""
    from api.workers.dispatch import LEASE

    org, website_id, conn = site
    claimed, _ = await tick(conn)
    run = next(e for e in claimed if e.website_id == website_id)
    await mark_running(conn, run.run_id)

    async def status_of() -> dict:
        return await (
            await conn.execute(
                "select status, error, finished_at from scheduled_runs"
                " where id = %s",
                (run.run_id,),
            )
        ).fetchone()

    # Nothing is reaped while the lease holds. Asserted on this run rather
    # than on the reaper's total, because the table is shared with every
    # other test in the suite.
    await reap(conn, now=datetime.now(UTC))
    assert (await status_of())["status"] == "running"

    await reap(conn, now=datetime.now(UTC) + LEASE + timedelta(minutes=1))
    row = await status_of()
    assert row["status"] == "failed"
    assert row["error"] == "the worker did not report back"
    assert row["finished_at"] is not None


async def test_a_slot_no_worker_ever_took_says_so(site):
    """A different sentence from "the worker died", because it means
    something different to whoever is on call: the pool is down, or the queue
    is so far behind that the night ran out."""
    from api.workers.dispatch import LEASE

    org, website_id, conn = site
    claimed, _ = await tick(conn)
    run = next(e for e in claimed if e.website_id == website_id)

    await reap(conn, now=datetime.now(UTC) + LEASE + timedelta(minutes=1))

    row = await (
        await conn.execute(
            "select status, error from scheduled_runs where id = %s", (run.run_id,)
        )
    ).fetchone()
    assert (row["status"], row["error"]) == ("failed", "no worker picked this up")


async def test_a_reaped_slot_does_not_block_the_next_night(site):
    org, website_id, conn = site
    await tick(conn)
    await reap(conn, now=datetime.now(UTC) + timedelta(hours=7))

    tomorrow, _ = await tick(conn, now=WEDNESDAY + timedelta(days=1))
    assert [e for e in tomorrow if e.website_id == website_id]


async def test_a_claim_is_visible_to_another_connection_before_it_is_queued(site):
    """The bug this test exists for, and it is invisible without it.

    If a task is enqueued inside the transaction that created its claim row,
    a worker can pick the task up before that transaction commits. It looks up
    a row that does not exist yet, concludes somebody else owns it, and
    returns without doing the work — leaving the slot claimed forever and the
    customer's data unrefreshed. Nothing errors, nothing logs, and the only
    symptom is a dashboard that stops updating.

    So the assertion is made from a SEPARATE connection, which is what a
    worker is.
    """
    import os

    import psycopg
    from psycopg.rows import dict_row

    org, website_id, conn = site
    seen: list[tuple[int, str]] = []

    async with await psycopg.AsyncConnection.connect(
        os.environ.get("SERVICE_DATABASE_URL", os.environ["DATABASE_URL"]),
        autocommit=True,
        row_factory=dict_row,
    ) as worker:

        async def enqueue(entry):
            row = await (
                await worker.execute(
                    "select id, status from scheduled_runs where id = %s",
                    (entry.run_id,),
                )
            ).fetchone()
            assert row is not None, (
                f"run {entry.run_id} was queued before its claim was committed"
            )
            seen.append((row["id"], row["status"]))

        await dispatch(conn, now=WEDNESDAY, enqueue=enqueue)

    assert seen, "nothing was dispatched"
    assert all(status == "claimed" for _, status in seen)


async def test_the_background_connection_commits_as_it_goes(client):
    """What `dispatch` and every job depend on.

    `service_session` holds one transaction for its whole scope, which is
    right for a request and wrong for a job: a 500-page crawl would be one
    long transaction, and a failure would roll back the row recording the
    failure. `service_task` is the same service role without the wrapper.
    """
    async with db.service_task() as conn:
        assert conn.autocommit is True

    async with db.service_session() as conn:
        assert conn.info.transaction_status.name in {"INTRANS", "INERROR"}
