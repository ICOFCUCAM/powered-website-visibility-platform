"""The worker: one deploy loop, one job loop, one scheduler sweep.

Deploys run strictly one at a time. A build saturates CPU and disk, and two
concurrent ones on a single host make both slower than running them in
sequence while also letting either starve the other of memory.

Scheduled jobs run in their own loop, concurrently with deploys and with each
other up to a small limit. They belong on a different loop because the worker
process is only *waiting* on them — the work happens inside another container
— and because a fifteen-minute nightly job sharing the deploy loop would block
every deploy for fifteen minutes.

Scaling up means running a second worker process. The leases in the database
already make that safe for both loops.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time

from forge.adapters import db
from forge.config import get_settings
from forge.engine import pipeline, processes, scheduler, service
from forge.repositories import deployments as deployment_repo
from forge.repositories import processes as process_repo

logger = logging.getLogger("forge.worker")

#: How long to wait when a queue is empty. Short enough that a deploy feels
#: immediate, long enough that an idle platform is not querying constantly.
IDLE_POLL_SECONDS = 2.0

#: How often to look for work whose worker died.
SWEEP_INTERVAL_SECONDS = 60.0

#: How often to turn due schedules into rows. Well under a minute, because a
#: schedule's smallest unit is a minute and a sweep that ran every 90 seconds
#: would run some minutes' jobs late and others not at all.
SCHEDULE_INTERVAL_SECONDS = 20.0

#: Concurrent scheduled jobs per worker. Three rather than one so a slow
#: nightly job does not delay every other project's, and rather than many
#: because each one is a container competing for the same host.
JOB_CONCURRENCY = 3


def worker_id() -> str:
    """Host and PID — what someone debugging a stuck deployment needs in order
    to go and look at it."""
    return f"{socket.gethostname()}/{os.getpid()}"


class Worker:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._id = worker_id()
        self._stopping = asyncio.Event()
        self._last_reclaim = 0.0
        self._last_schedule = 0.0
        self._jobs: set[asyncio.Task] = set()

    def request_stop(self) -> None:
        """Finish what is in hand, then exit.

        Not a cancellation. Killing a build halfway leaves a half-written
        image and a deployment stuck in `building`; letting it finish costs at
        most one build's time and leaves the database consistent.
        """
        if not self._stopping.is_set():
            logger.info("shutdown requested — finishing work in hand")
            self._stopping.set()

    async def run(self) -> None:
        settings = self._settings
        await db.open_pool(
            settings.database_url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
        )
        try:
            await self._startup_reconcile()
            logger.info("worker %s ready", self._id)
            await asyncio.gather(self._deploy_loop(), self._job_loop())
        finally:
            await self._drain_jobs()
            await db.close_pool()
            logger.info("worker %s stopped", self._id)

    async def _startup_reconcile(self) -> None:
        report = await service.reconcile(self._settings)
        logger.info(
            "reconciled with docker: %s container(s) gone, %s abandoned "
            "deployment(s) failed, %s project(s) with production down",
            report["detached"],
            report["abandoned"],
            report["production_down"],
        )
        if report["production_down"]:
            logger.warning(
                "%s project(s) have a production deployment whose container is "
                "missing — roll back or redeploy them",
                report["production_down"],
            )

    # -- deploys ------------------------------------------------------------

    async def _deploy_loop(self) -> None:
        while not self._stopping.is_set():
            await self._reclaim()
            deployment = await deployment_repo.claim_next(self._id)
            if deployment is None:
                await self._idle()
                continue

            logger.info(
                "building %s #%s (%s)",
                deployment.short_id,
                deployment.number,
                deployment.git_sha[:8],
            )
            started = time.monotonic()
            finished = await pipeline.run_deployment(deployment, settings=self._settings)
            logger.info(
                "%s #%s finished as %s in %.1fs",
                finished.short_id,
                finished.number,
                finished.status.value,
                time.monotonic() - started,
            )

    # -- scheduled jobs -----------------------------------------------------

    async def _job_loop(self) -> None:
        while not self._stopping.is_set():
            await self._schedule()

            if len(self._jobs) >= JOB_CONCURRENCY:
                await self._idle()
                continue

            run = await process_repo.claim_next_run(self._id)
            if run is None:
                await self._idle()
                continue

            task = asyncio.create_task(self._execute(run))
            self._jobs.add(task)
            task.add_done_callback(self._jobs.discard)

    async def _execute(self, run) -> None:
        try:
            process = await process_repo.get(run.process_id)
        except Exception:
            logger.exception("job run %s has no process", run.id)
            return

        logger.info(
            "running job %s for slot %s", process.name, run.scheduled_for.isoformat()
        )
        started = time.monotonic()
        finished = await processes.run_job(run, process, settings=self._settings)
        level = logging.INFO if finished.status.value == "succeeded" else logging.WARNING
        logger.log(
            level,
            "job %s finished as %s in %.1fs",
            process.name,
            finished.status.value,
            time.monotonic() - started,
        )

    async def _drain_jobs(self) -> None:
        """Let running jobs finish before the pool closes under them."""
        if not self._jobs:
            return
        logger.info("waiting for %s job(s) to finish", len(self._jobs))
        await asyncio.gather(*list(self._jobs), return_exceptions=True)

    # -- periodic -----------------------------------------------------------

    async def _schedule(self) -> None:
        now = time.monotonic()
        if now - self._last_schedule < SCHEDULE_INTERVAL_SECONDS:
            return
        self._last_schedule = now
        try:
            await scheduler.sweep()
        except Exception:
            # A broken sweep must not end the loop that also executes jobs.
            logger.exception("schedule sweep failed")

    async def _reclaim(self) -> None:
        now = time.monotonic()
        if now - self._last_reclaim < SWEEP_INTERVAL_SECONDS:
            return
        self._last_reclaim = now

        for deployment in await deployment_repo.reclaim_abandoned(
            older_than_seconds=self._settings.build_timeout_seconds + 120
        ):
            logger.warning(
                "failed abandoned deployment %s #%s",
                deployment.short_id,
                deployment.number,
            )
        # Generous, because the bound is per job and the longest legitimate
        # timeout is the one configured on the slowest process.
        for run in await process_repo.reclaim_abandoned_runs(older_than_seconds=7200):
            logger.warning("failed abandoned job run %s", run.id)

    async def _idle(self) -> None:
        """Sleep, but wake immediately on shutdown.

        A plain sleep would make SIGTERM take up to the poll interval to be
        noticed, turning every restart into a needless pause.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=IDLE_POLL_SECONDS)


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)
    await worker.run()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
