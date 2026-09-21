"""When each job runs, for each website.

Pure arithmetic over a clock and a website id: no database, no broker, no
Celery. That is deliberate — the schedule is the part most likely to be wrong
in a way nobody notices for a month, so it is the part that has to be testable
without standing anything up.

Two decisions carry most of the weight.

**Every website gets its own minute.** The V1 spec puts the nightly work in a
01:00–05:00 window; firing all of it at exactly 01:00 would hand Google a
thundering herd, collect a wall of 429s, and spend the night's quota on
retries. So each website's slot is its base hour plus an offset derived from
its id, spreading the fleet across the hour.

**The offset is a hash of the id, not `hash()`.** Python salts `hash()` per
process unless PYTHONHASHSEED is fixed, so a website's slot would move every
time a worker restarted — which is exactly the kind of drift that makes "did
last night run?" unanswerable. SHA-256 of the id's bytes is stable forever,
across processes, machines and releases.

**A missed window is late, not lost.** `due_slot` returns the most recent slot
that has passed, whether that was twenty minutes ago or yesterday, so a pool
that was down overnight catches up on the next tick. Exactly one slot, so an
outage of a week does not produce a week of work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from uuid import UUID

#: How often the dispatcher ticks. Every slot is therefore claimed within this
#: long of its due time, and the value is small enough that "the schedule is
#: broken" is noticed in minutes rather than the next morning.
TICK = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    #: The Celery queue, which is also the worker pool (docs/08-architecture).
    queue: str
    hour: int
    #: The minute range the stagger spreads across. Half-open: [from, to).
    minute_from: int = 0
    minute_to: int = 60
    #: 0 = Monday. None means every day.
    weekday: int | None = None

    def slot_minute(self, website_id: UUID) -> int:
        span = self.minute_to - self.minute_from
        return self.minute_from + (_offset(website_id) % span)

    def runs_on(self, day: datetime) -> bool:
        return self.weekday is None or day.weekday() == self.weekday


def _offset(website_id: UUID) -> int:
    """Stable across processes and releases. See the module docstring."""
    return int.from_bytes(sha256(website_id.bytes).digest()[:4], "big")


#: The V1 spec's nightly schedule (§31), as laid out in docs/08-architecture.md.
#:
#:   01:00  sync_search_console      04:00  calculate_scores
#:   02:00  sync_analytics           04:30  generate_recommendations
#:   03:00  crawl_website            Mon 06:00  generate_weekly_report
#:
#: Ordered as the night runs them, because the dependencies are real: scores
#: need the crawl, and recommendations need the scores.
JOBS: tuple[Job, ...] = (
    Job("sync_search_console", queue="sync", hour=1),
    Job("sync_analytics", queue="sync", hour=2),
    Job("crawl_website", queue="crawl", hour=3),
    Job("calculate_scores", queue="analysis", hour=4),
    # Kept inside its own half hour rather than staggered across a full one,
    # so it cannot spill past 05:00 and collide with the morning.
    Job("generate_recommendations", queue="ai", hour=4, minute_from=30),
    Job("generate_weekly_report", queue="reports", hour=6, weekday=0),
)

BY_NAME: dict[str, Job] = {job.name: job for job in JOBS}


def slot_on(job: Job, website_id: UUID, day: datetime) -> datetime:
    """The exact moment this job is due for this website on this day."""
    return datetime.combine(
        day.date(),
        time(hour=job.hour, minute=job.slot_minute(website_id)),
        tzinfo=UTC,
    )


def due_slot(job: Job, website_id: UUID, now: datetime) -> datetime | None:
    """The most recent slot at or before `now`, or None if there is not one.

    Walks back a bounded number of days so a weekly job still finds its last
    Monday, and returns the FIRST match — the latest slot only. Catching up on
    every missed slot would turn a long outage into a stampede.
    """
    now = _utc(now)
    for days_back in range(0, 8):
        day = now - timedelta(days=days_back)
        if not job.runs_on(day):
            continue
        slot = slot_on(job, website_id, day)
        if slot <= now:
            return slot
    return None


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def next_slot(job: Job, website_id: UUID, now: datetime) -> datetime:
    """The next slot strictly after `now`. For telling a customer when their
    data refreshes next, which is a question the dashboard should answer."""
    now = _utc(now)
    for days_ahead in range(0, 8):
        day = now + timedelta(days=days_ahead)
        if not job.runs_on(day):
            continue
        slot = slot_on(job, website_id, day)
        if slot > now:
            return slot
    raise AssertionError("a weekly job has a slot within eight days")  # pragma: no cover
