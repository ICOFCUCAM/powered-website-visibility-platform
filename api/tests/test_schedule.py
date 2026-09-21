"""The nightly schedule's arithmetic.

No database, no broker, no Celery — which is the point. The schedule is the
part most likely to be wrong in a way nobody notices for a month, so it is the
part that has to be checkable in milliseconds.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from api.workers.schedule import BY_NAME, JOBS, due_slot, next_slot, slot_on

WEBSITE = UUID("7f081cbe-b9ed-4898-bd39-abcc5546a851")
WEDNESDAY = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
MONDAY = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


# -- the stagger ------------------------------------------------------------
def test_the_stagger_is_stable_across_processes():
    """The reason this is a SHA-256 of the id and not `hash()`.

    Python salts `hash()` per process unless PYTHONHASHSEED is fixed, so a
    website's slot would move every time a worker restarted — and "did last
    night's sync run?" would stop having an answer.
    """
    script = (
        "from uuid import UUID;"
        "from api.workers.schedule import BY_NAME;"
        f"print(BY_NAME['crawl_website'].slot_minute(UUID('{WEBSITE}')))"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        ).stdout.strip()
        for seed in ("0", "1", "random")
    }
    assert len(runs) == 1, f"the slot moved between processes: {runs}"
    assert runs == {str(BY_NAME["crawl_website"].slot_minute(WEBSITE))}


def test_the_fleet_is_spread_across_the_hour():
    """Firing every website at exactly 01:00 hands Google a thundering herd
    and spends the night's quota on retries."""
    job = BY_NAME["sync_search_console"]
    minutes = {job.slot_minute(uuid4()) for _ in range(400)}
    assert len(minutes) > 50, "the stagger is not spreading the fleet"
    assert min(minutes) >= 0 and max(minutes) <= 59


def test_every_slot_lands_inside_its_window():
    """The V1 spec puts the nightly work in 01:00-05:00. The 04:30 job is
    staggered across its own half hour so it cannot spill past 05:00 into the
    morning."""
    for _ in range(200):
        website = uuid4()
        for job in JOBS:
            slot = slot_on(job, website, WEDNESDAY)
            assert slot.hour == job.hour
            assert job.minute_from <= slot.minute < job.minute_to
        late = slot_on(BY_NAME["generate_recommendations"], website, WEDNESDAY)
        assert late.minute >= 30


# -- what is due ------------------------------------------------------------
def test_before_today_s_slot_the_due_one_is_yesterday_s():
    job = BY_NAME["crawl_website"]  # 03:00 + stagger
    at_two_am = datetime(2026, 9, 23, 2, 0, tzinfo=UTC)

    due = due_slot(job, WEBSITE, at_two_am)
    assert due is not None and due.date() == (at_two_am - timedelta(days=1)).date()
    assert due < at_two_am


def test_after_today_s_slot_the_due_one_is_today_s():
    job = BY_NAME["crawl_website"]
    due = due_slot(job, WEBSITE, WEDNESDAY)
    assert due is not None and due.date() == WEDNESDAY.date()


def test_an_overnight_outage_catches_up_exactly_one_slot():
    """A missed window is late, not lost — and not a stampede either. A pool
    down from 01:00 to 09:00 runs last night's sync at 09:05; a pool down for
    a week still runs one."""
    job = BY_NAME["sync_search_console"]
    after_a_week = WEDNESDAY + timedelta(days=7)

    due = due_slot(job, WEBSITE, after_a_week)
    assert due is not None and due.date() == after_a_week.date()
    assert (after_a_week - due).days == 0


def test_the_weekly_report_only_comes_due_on_a_monday():
    job = BY_NAME["generate_weekly_report"]
    assert job.weekday == 0

    monday_due = due_slot(job, WEBSITE, MONDAY.replace(hour=9))
    assert monday_due is not None and monday_due.weekday() == 0
    assert monday_due.date() == MONDAY.date()

    # On Wednesday the most recent slot is still Monday's, already claimed.
    wednesday_due = due_slot(job, WEBSITE, WEDNESDAY)
    assert wednesday_due == monday_due


def test_nothing_is_due_before_the_first_slot_of_the_week():
    """A Monday morning before 06:00 has no report slot behind it this week,
    so it finds last Monday's."""
    job = BY_NAME["generate_weekly_report"]
    early = MONDAY.replace(hour=5, minute=0)
    due = due_slot(job, WEBSITE, early)
    assert due is not None and due.date() == (MONDAY - timedelta(days=7)).date()


def test_the_next_slot_is_strictly_in_the_future():
    """The dashboard should be able to say when a customer's data refreshes
    next, and "now" is not an answer."""
    for job in JOBS:
        upcoming = next_slot(job, WEBSITE, WEDNESDAY)
        assert upcoming > WEDNESDAY
        assert job.runs_on(upcoming)


def test_a_naive_clock_is_read_as_utc_rather_than_crashing():
    job = BY_NAME["crawl_website"]
    assert due_slot(job, WEBSITE, datetime(2026, 9, 23, 9, 0)) == due_slot(
        job, WEBSITE, WEDNESDAY
    )


# -- the schedule itself ----------------------------------------------------
def test_the_schedule_matches_the_frozen_architecture():
    """docs/08-architecture.md:

        01:00  sync_search_console      04:00  calculate_scores
        02:00  sync_analytics           04:30  generate_recommendations
        03:00  crawl_website            Mon 06:00  generate_weekly_report
    """
    assert [(job.name, job.hour, job.minute_from, job.weekday) for job in JOBS] == [
        ("sync_search_console", 1, 0, None),
        ("sync_analytics", 2, 0, None),
        ("crawl_website", 3, 0, None),
        ("calculate_scores", 4, 0, None),
        ("generate_recommendations", 4, 30, None),
        ("generate_weekly_report", 6, 0, 0),
    ]


def test_the_night_runs_in_dependency_order():
    """Scores need the crawl; recommendations need the scores. A schedule that
    ran them in the wrong order would report on yesterday every day."""
    order = [job.name for job in JOBS]
    assert order.index("crawl_website") < order.index("calculate_scores")
    assert order.index("calculate_scores") < order.index("generate_recommendations")
    assert order.index("sync_search_console") < order.index("calculate_scores")


def test_each_job_names_a_pool_that_exists():
    from api.workers.app import POOL_CONCURRENCY

    for job in JOBS:
        assert job.queue in POOL_CONCURRENCY, job.name
