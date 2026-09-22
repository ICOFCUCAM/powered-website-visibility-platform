"""Deployment logs, buffered on the way to the database.

A Docker build emits a few hundred lines in a few seconds. One INSERT per line
would make the database round trip, not the build, the slowest part of a
deploy — so lines are collected and flushed on whichever comes first: enough
lines to be worth a write, or enough time that someone watching the log would
notice the pause.
"""

from __future__ import annotations

import asyncio
import time
from uuid import UUID

from forge.domain.models import LogStream
from forge.repositories import deployments as deployment_repo

#: Flush thresholds. 40 lines is roughly one screen; 400ms is below the point
#: at which a live log feels like it has stalled.
MAX_BUFFERED = 40
MAX_DELAY_SECONDS = 0.4

#: Individual lines are truncated rather than rejected. A minified stack trace
#: on one line can run to megabytes, and storing it whole would push the useful
#: lines around it out of any reasonable page.
MAX_LINE = 4000


class LogWriter:
    """Append-only log for one deployment.

    Holds the sequence number in memory rather than reading MAX(seq) per line,
    which is the difference between one query per deploy and one per line. It
    is initialised from the database so that a retry of a deployment continues
    the numbering instead of colliding with what is already there.
    """

    def __init__(self, deployment_id: UUID, *, start_seq: int = 0) -> None:
        self._deployment_id = deployment_id
        self._seq = start_seq
        self._buffer: list[tuple[int, LogStream, str]] = []
        self._last_flush = time.monotonic()
        self._lock = asyncio.Lock()

    @classmethod
    async def resume(cls, deployment_id: UUID) -> LogWriter:
        start = await deployment_repo.last_log_seq(deployment_id)
        return cls(deployment_id, start_seq=start)

    async def write(self, line: str, *, stream: LogStream = LogStream.BUILD) -> None:
        async with self._lock:
            self._seq += 1
            self._buffer.append((self._seq, stream, line[:MAX_LINE]))
            due = (
                len(self._buffer) >= MAX_BUFFERED
                or time.monotonic() - self._last_flush >= MAX_DELAY_SECONDS
            )
        if due:
            await self.flush()

    async def system(self, line: str) -> None:
        """Forge's own narration: what it detected, what it is about to do.

        Kept on its own stream so the interesting three lines of a deploy are
        not lost among six hundred lines of npm output.
        """
        await self.write(line, stream=LogStream.SYSTEM)

    async def flush(self) -> None:
        async with self._lock:
            pending, self._buffer = self._buffer, []
            self._last_flush = time.monotonic()
        await deployment_repo.append_logs(self._deployment_id, pending)

    def sink(self, stream: LogStream = LogStream.BUILD):
        """A one-argument coroutine, for adapters that take a log callback."""

        async def write(line: str) -> None:
            await self.write(line, stream=stream)

        return write
