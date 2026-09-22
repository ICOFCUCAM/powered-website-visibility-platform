"""Deciding which scheduled jobs are due.

The sweep does not run anything. It turns "this schedule names 03:00 and it is
now 03:00" into a row, and the row is claimed and executed by the same queue
machinery the deploy pipeline uses. Separating the two is what makes a missed
window recoverable: the slot exists as a fact in the database whether or not a
worker was alive to notice it at the time.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from forge.domain.models import JobStatus, Process
from forge.domain.schedule import InvalidSchedule, parse
from forge.repositories import processes as process_repo
from forge.repositories import projects as project_repo

logger = logging.getLogger("forge.scheduler")


async def sweep(*, now: datetime | None = None) -> int:
    """Enqueue every cron slot that has come due. Returns how many were new.

    Safe to call from several workers at once, and safe to call far more often
    than any schedule fires: a slot already enqueued conflicts on
    `(process_id, scheduled_for)` and is silently skipped, so the sweep is
    idempotent by construction rather than by timing.
    """
    moment = now or datetime.now(UTC)
    enqueued = 0

    for process in await process_repo.list_scheduled():
        slot = _due_slot(process, moment)
        if slot is None:
            continue

        project = await project_repo.get(process.project_id)
        runnable = project.production_deployment_id is not None

        # A job with nothing to run is written straight to its final state.
        # Recorded rather than dropped, so the history shows the slot was due
        # and why it did not run: a silent gap looks like a broken scheduler.
        run = await process_repo.claim_slot(
            process.id,
            slot,
            project.production_deployment_id,
            status=JobStatus.PENDING.value if runnable else JobStatus.SKIPPED.value,
            detail=None if runnable else "Nothing is serving production yet.",
        )
        if run is None:
            # Already enqueued — by an earlier sweep, or by another worker in
            # the same second. Not an error; it is the mechanism working.
            continue

        enqueued += 1
        logger.info(
            "%s %s/%s for %s",
            "queued" if runnable else "skipped",
            project.slug,
            process.name,
            slot.isoformat(),
        )

    return enqueued


def _due_slot(process: Process, now: datetime):
    """The slot this process owes, or None.

    An unparseable schedule is logged and skipped rather than raised. One
    project's typo must not stop every other project's jobs, and the schedule
    was validated when it was saved — reaching here means it was changed in
    the database by hand.
    """
    try:
        schedule = parse(process.schedule or "")
    except InvalidSchedule as exc:
        logger.error(
            "process %s has an unusable schedule %r: %s",
            process.id,
            process.schedule,
            exc,
        )
        return None
    return schedule.due_slot(now)
