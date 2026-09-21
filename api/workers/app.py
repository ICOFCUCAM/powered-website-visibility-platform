"""The Celery application: queues, pools, and the beat tick.

Deliberately thin. Every task below is four lines wrapping an async function
in `api/workers/jobs.py`, because the moment scheduling logic lives inside a
Celery task it can only be tested by standing up a broker — and a nightly
schedule that is hard to test is a nightly schedule nobody trusts.

**Six pools, separated by job shape** (docs/08-architecture.md). A single
queue is how one agency's forty-website backfill makes every other customer's
dashboard look broken: crawls are long and host-rate-limited, syncs are
quota-bound and back-off-heavy, analysis is short and CPU-bound. Sharing one
pool between them means the short work waits behind the long work.

**Celery is sync; this codebase is async.** Each task runs its coroutine on a
per-process event loop that is created once and reused, rather than a fresh
`asyncio.run` per task — the database pool is bound to a loop, and a new loop
per task would discard the pool with it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init, worker_process_shutdown

from api.adapters import db
from api.config import get_settings
from api.workers import jobs
from api.workers.dispatch import Claimed, dispatch, reap
from api.workers.schedule import TICK

logger = logging.getLogger("visibility_hub.workers")

BROKER_URL = os.environ.get("CELERY_BROKER_URL") or os.environ.get(
    "REDIS_URL", "redis://127.0.0.1:6379/0"
)

app = Celery("visibility_hub", broker=BROKER_URL, backend=None)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # An unacknowledged task is redelivered if a worker dies mid-crawl. That
    # is safe here only because `mark_running` refuses a second start — see
    # api/workers/dispatch.py.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="analysis",
    task_routes={
        "jobs.sync_search_console": {"queue": "sync"},
        "jobs.sync_analytics": {"queue": "sync"},
        "jobs.crawl_website": {"queue": "crawl"},
        "jobs.calculate_scores": {"queue": "analysis"},
        "jobs.generate_recommendations": {"queue": "ai"},
        "jobs.generate_weekly_report": {"queue": "reports"},
        "scheduler.tick": {"queue": "analysis"},
        "scheduler.ensure_partitions": {"queue": "analysis"},
    },
    beat_schedule={
        # The dispatcher, not the jobs. Beat fires a tick; the tick works out
        # what is actually due per website and claims it. That indirection is
        # what lets every website have its own minute, and what lets a missed
        # window be caught up rather than skipped.
        "tick": {
            "task": "scheduler.tick",
            # Overridable so a developer can watch the loop turn without
            # waiting five minutes for each pass. Production leaves it alone:
            # every slot is then claimed within five minutes of its due time,
            # which is close enough for a nightly schedule and rare enough
            # that the tick costs nothing.
            "schedule": float(
                os.environ.get("SCHEDULER_TICK_SECONDS", TICK.total_seconds())
            ),
        },
        # Stays three months ahead of the fact tables. 00:20 UTC, before the
        # night's first sync at 01:00 and well clear of it.
        "partitions": {
            "task": "scheduler.ensure_partitions",
            "schedule": crontab(hour=0, minute=20),
        },
    },
)

#: Concurrency per pool, from docs/08-architecture.md. Read by the worker
#: launcher rather than set here, because Celery takes it per process.
POOL_CONCURRENCY = {
    "crawl": 8,
    "render": 2,
    "sync": 4,
    "analysis": 4,
    "ai": 2,
    "reports": 2,
}

_loop: asyncio.AbstractEventLoop | None = None


@worker_process_init.connect
def _open_pools(**_: Any) -> None:
    """One event loop and one database pool per worker process.

    Celery forks its processes, and a pool inherited across a fork is a pool
    whose sockets two processes both believe they own. Opening after the fork
    is the only safe moment.
    """
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    settings = get_settings()
    _loop.run_until_complete(
        db.open_pool(
            settings.database_url,
            min_size=1,
            max_size=settings.pool_max_size,
            service_dsn=settings.service_database_url,
        )
    )


@worker_process_shutdown.connect
def _close_pools(**_: Any) -> None:
    global _loop
    if _loop is None:
        return
    _loop.run_until_complete(db.close_pool())
    _loop.close()
    _loop = None


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    if _loop is None:  # pragma: no cover - only outside a worker process
        return asyncio.run(coro)
    return _loop.run_until_complete(coro)


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------
@app.task(name="scheduler.tick")
def tick() -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        # Autocommit: a claim must be visible to a worker before the task
        # that needs it is on the queue. See `dispatch`.
        async with db.service_task() as conn:
            now = datetime.now(UTC)
            # Before claiming anything new: close out the runs nobody is
            # coming back for, so a stuck row is a visible failure rather
            # than a row that stays 'running' until somebody reads the table.
            reaped = await reap(conn, now=now)
            claimed = await dispatch(conn, now=now, enqueue=_enqueue)
        return {"claimed": len(claimed), "reaped": reaped}

    return run(go())


def _enqueue(entry: Claimed) -> None:
    app.send_task(
        f"jobs.{entry.job}",
        args=[entry.run_id],
        queue=entry.queue,
        # A slot that has not started within its own window has been overtaken
        # by the next one. Expiring it is better than running two nights of
        # the same job back to back at breakfast.
        expires=60 * 60 * 20,
    )


@app.task(name="scheduler.ensure_partitions")
def ensure_partitions() -> dict[str, Any]:
    return run(jobs.ensure_partitions())


# ---------------------------------------------------------------------------
# The jobs
# ---------------------------------------------------------------------------
def _task(name: str):
    job = jobs.BY_NAME[name]

    @app.task(name=f"jobs.{name}", bind=True, max_retries=2, default_retry_delay=300)
    def task(self, run_id: int) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        try:
            return run(job(run_id))
        except Exception as exc:
            # Two retries, five minutes apart, and then it stays failed. A
            # nightly job that retries forever spends the night hammering
            # whatever broke — usually Google, usually while it is rate
            # limiting us.
            raise self.retry(exc=exc) from exc

    return task


sync_search_console = _task("sync_search_console")
sync_analytics = _task("sync_analytics")
crawl_website = _task("crawl_website")
calculate_scores = _task("calculate_scores")
generate_recommendations = _task("generate_recommendations")
generate_weekly_report = _task("generate_weekly_report")
