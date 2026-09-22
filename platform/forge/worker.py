"""The deploy worker.

A loop that claims one deployment at a time and runs it to completion. One at
a time on purpose: a build saturates CPU and disk, and two concurrent ones on
a single host make both slower than running them in sequence while also making
either able to starve the other of memory. Scaling up means running a second
worker process, which the lease in the database already makes safe.
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
from forge.engine import pipeline, service
from forge.repositories import deployments as deployment_repo

logger = logging.getLogger("forge.worker")

#: How long to wait when the queue is empty. Short enough that a deploy feels
#: immediate, long enough that an idle platform is not querying constantly.
IDLE_POLL_SECONDS = 2.0

#: How often to look for deployments whose worker died.
SWEEP_INTERVAL_SECONDS = 60.0


def worker_id() -> str:
    """Identifies the lease holder. Host and PID, because that is what someone
    debugging a stuck deployment needs in order to go and look at it."""
    return f"{socket.gethostname()}/{os.getpid()}"


class Worker:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._id = worker_id()
        self._stopping = asyncio.Event()
        self._last_sweep = 0.0

    def request_stop(self) -> None:
        """Finish the deployment in hand, then exit.

        Not a cancellation. Killing a build halfway leaves a half-written image
        and a deployment stuck in `building`; letting it finish costs at most
        one build's time and leaves the database consistent.
        """
        if not self._stopping.is_set():
            logger.info("shutdown requested — finishing the current deployment")
            self._stopping.set()

    async def run(self) -> None:
        settings = self._settings
        await db.open_pool(
            settings.database_url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
        )
        try:
            report = await service.reconcile(settings)
            logger.info(
                "reconciled with docker: %s container(s) gone, %s abandoned "
                "deployment(s) failed, %s project(s) with production down",
                report["detached"],
                report["abandoned"],
                report["production_down"],
            )
            if report["production_down"]:
                logger.warning(
                    "%s project(s) have a production deployment whose container "
                    "is missing — roll back or redeploy them",
                    report["production_down"],
                )

            logger.info("worker %s ready", self._id)
            while not self._stopping.is_set():
                worked = await self._tick()
                if not worked:
                    await self._idle()
        finally:
            await db.close_pool()
            logger.info("worker %s stopped", self._id)

    async def _tick(self) -> bool:
        await self._sweep()
        deployment = await deployment_repo.claim_next(self._id)
        if deployment is None:
            return False

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
        return True

    async def _sweep(self) -> None:
        now = time.monotonic()
        if now - self._last_sweep < SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        abandoned = await deployment_repo.reclaim_abandoned(
            older_than_seconds=self._settings.build_timeout_seconds + 120
        )
        for deployment in abandoned:
            logger.warning(
                "failed abandoned deployment %s #%s",
                deployment.short_id,
                deployment.number,
            )

    async def _idle(self) -> None:
        """Sleep, but wake immediately on shutdown.

        A plain sleep would make SIGTERM take up to the poll interval to be
        noticed, which turns every restart into a needless pause.
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
