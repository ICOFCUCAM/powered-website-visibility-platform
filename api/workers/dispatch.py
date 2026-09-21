"""Deciding what is due, and claiming the right to run it.

Everything about this module exists to survive the two ways a nightly schedule
actually fails: it runs twice, or it silently does not run at all.

  RUNNING TWICE is prevented by the unique index on
  `(website_id, job, window_start)`. Claiming a slot IS inserting that row, so
  two dispatchers racing on the same tick produce one winner and one no-op.
  No lock, no leader election, no Redis key that can expire mid-crawl.

  NOT RUNNING AT ALL is prevented by claiming the most recent slot that has
  PASSED rather than reacting to a cron tick. A pool that was down from 01:00
  to 09:00 claims last night's slot at 09:05 and runs it late. Exactly one
  slot, so a week's outage is not a week of backlog.

Eligibility is checked before claiming, not inside the job. A website with no
Search Console link does not need a failed sync row every night to tell it so,
and an operator scanning for red should see problems rather than absences.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one
from api.workers.schedule import JOBS, Job, due_slot

logger = logging.getLogger("visibility_hub.workers")

#: How long a run may sit claimed or running before it is treated as
#: abandoned.
#:
#: This is the same problem the crawl frontier solved with leases, and the
#: same answer: a slot whose worker died must become visible WITHOUT requiring
#: that worker to come back and admit it. A row left `running` forever is
#: worse than a failure, because a failure is something an operator can see.
#:
#: Generous on purpose. Its only job is to tell "in flight" from "abandoned",
#: and the cost of being wrong in the impatient direction is two workers
#: crawling the same site. The next night's slot is a fresh claim regardless,
#: so nothing waits on this to recover.
LEASE = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One website, with the facts every eligibility rule needs."""

    website_id: UUID
    organization_id: UUID
    domain: str
    crawl_allowed: bool
    has_search_console: bool
    has_analytics: bool
    has_crawl: bool


@dataclass(frozen=True, slots=True)
class Claimed:
    run_id: int
    job: str
    queue: str
    website_id: UUID
    organization_id: UUID
    window_start: datetime


#: What each job needs before it is worth running. Returning False is a
#: SKIP, not a failure: there is nothing wrong with a website that has not
#: connected Analytics yet, and a nightly failed row would say there was.
def _eligible(job: Job, site: Candidate) -> bool:
    if job.name == "sync_search_console":
        return site.has_search_console
    if job.name == "sync_analytics":
        return site.has_analytics
    if job.name == "crawl_website":
        # Decision 20: derived, never user-controlled, and checked again by the
        # crawler before it fetches. Checking here too just avoids queueing
        # work that is going to refuse itself.
        return site.crawl_allowed
    # Scores, recommendations and the weekly report all describe a crawl. Run
    # before the first one lands, they would report on nothing and the
    # customer's first score would be a fiction.
    return site.has_crawl


async def candidates(conn: AsyncConnection) -> list[Candidate]:
    """Every website the schedule might act on, in one query.

    One query rather than one per website per job: at a thousand websites and
    six jobs that is the difference between six thousand round trips per tick
    and one.
    """
    rows = await fetch_all(
        conn,
        """
        select w.id, w.organization_id, w.domain, w.crawl_allowed,
               exists(select 1 from website_connections l
                        join connection_properties p on p.id = l.property_id
                        join connections c on c.id = p.connection_id
                       where l.website_id = w.id and l.status = 'active'
                         and l.service = 'search_console'
                         and c.status = 'active')          as has_search_console,
               exists(select 1 from website_connections l
                        join connection_properties p on p.id = l.property_id
                        join connections c on c.id = p.connection_id
                       where l.website_id = w.id and l.status = 'active'
                         and l.service = 'analytics'
                         and c.status = 'active')          as has_analytics,
               exists(select 1 from crawls
                       where website_id = w.id and status = 'completed')
                                                            as has_crawl
          from websites w
         where w.archived_at is null
        """,
    )
    return [
        Candidate(
            website_id=row["id"],
            organization_id=row["organization_id"],
            domain=str(row["domain"]),
            crawl_allowed=bool(row["crawl_allowed"]),
            has_search_console=bool(row["has_search_console"]),
            has_analytics=bool(row["has_analytics"]),
            has_crawl=bool(row["has_crawl"]),
        )
        for row in rows
    ]


async def claim(
    conn: AsyncConnection,
    *,
    job: str,
    site: Candidate,
    window_start: datetime,
    trigger: str = "schedule",
) -> int | None:
    """Win the right to run one slot, or find it already taken.

    `on conflict do nothing returning id` is the whole mechanism: a row back
    means this process owns the slot; nothing back means another one does.
    """
    row = await fetch_one(
        conn,
        """
        insert into scheduled_runs (organization_id, website_id, job,
                                    window_start, trigger)
        values (%s, %s, %s, %s, %s)
        on conflict (website_id, job, window_start) do nothing
        returning id
        """,
        (site.organization_id, site.website_id, job, window_start, trigger),
    )
    return row["id"] if row else None


Enqueue = Callable[[Claimed], Awaitable[None] | None]


async def dispatch(
    conn: AsyncConnection,
    *,
    now: datetime | None = None,
    enqueue: Enqueue,
    jobs: tuple[Job, ...] = JOBS,
) -> list[Claimed]:
    """One tick. Claim everything due and hand it to the queue.

    THE CONNECTION MUST BE AUTOCOMMIT (`db.service_task`, not
    `db.service_session`), and that is not a style preference — it is the
    difference between working and not.

    Enqueue a task inside the transaction that created its claim row and a
    worker can pick the task up before that transaction commits. It then looks
    up a row that does not exist yet, decides somebody else must own it, and
    returns without doing the work — leaving the slot `claimed` forever and
    the customer's data unrefreshed. Nothing errors. It took a scheduler
    running against a database with three websites in it to see, because with
    three thousand the transaction was slow enough to lose the race.

    So each claim commits on its own, before it is handed to the queue. A tick
    is not an atomic unit and must not be one.

    `enqueue` is injected so the decision of what to run is testable without a
    broker — which matters, because the decision is the part with the bugs and
    the broker is the part without them.
    """
    now = now or datetime.now(UTC)
    sites = await candidates(conn)
    claimed: list[Claimed] = []

    for job in jobs:
        for site in sites:
            if not _eligible(job, site):
                continue
            window_start = due_slot(job, site.website_id, now)
            if window_start is None:
                continue

            run_id = await claim(
                conn, job=job.name, site=site, window_start=window_start
            )
            if run_id is None:
                continue

            entry = Claimed(
                run_id=run_id,
                job=job.name,
                queue=job.queue,
                website_id=site.website_id,
                organization_id=site.organization_id,
                window_start=window_start,
            )
            claimed.append(entry)
            result = enqueue(entry)
            if result is not None:
                await result

    if claimed:
        logger.info(
            "dispatched %d job(s): %s",
            len(claimed),
            ", ".join(sorted({entry.job for entry in claimed})),
        )
    return claimed


async def reap(
    conn: AsyncConnection, *, now: datetime | None = None, lease: timedelta = LEASE
) -> int:
    """Close out runs nobody is coming back for.

    Two shapes, and they mean different things to whoever is on call:

      `claimed` and never started — the task was enqueued and no worker ever
      took it. The pool is down, or the queue is so far behind that the night
      ran out.

      `running` and never finished — a worker took it and died. Celery
      redelivers the task, `mark_running` refuses the second start (rightly:
      we cannot tell a dead worker from a slow one), and without this the row
      would stay `running` until the table was read by a human.

    Marking them failed is what makes "is the schedule healthy?" a query
    rather than a hunch.
    """
    now = now or datetime.now(UTC)
    rows = await fetch_all(
        conn,
        """
        update scheduled_runs
           set status = 'failed', finished_at = now(),
               error = case when status = 'running'
                            then 'the worker did not report back'
                            else 'no worker picked this up' end
         where status in ('claimed', 'running')
           and coalesce(started_at, claimed_at) < %s
        returning id, job, status
        """,
        (now - lease,),
    )
    if rows:
        logger.warning("reaped %d abandoned scheduled run(s)", len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# The run's own lifecycle
# ---------------------------------------------------------------------------
async def mark_running(conn: AsyncConnection, run_id: int) -> dict[str, Any] | None:
    """Claim-to-running, and refuse a second start.

    A task delivered twice — a broker redelivery, a retry after a worker was
    killed mid-crawl — must not start the work again. The status predicate is
    what makes that impossible rather than unlikely.
    """
    return await fetch_one(
        conn,
        """
        update scheduled_runs
           set status = 'running', started_at = now()
         where id = %s and status = 'claimed'
        returning id, job, website_id, organization_id, window_start
        """,
        (run_id,),
    )


async def finish(
    conn: AsyncConnection,
    run_id: int,
    *,
    status: str,
    error: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        update scheduled_runs
           set status = %s, finished_at = now(), error = %s, detail = %s
         where id = %s
        """,
        (status, (error or None) and error[:1000], json.dumps(detail or {}), run_id),
    )
