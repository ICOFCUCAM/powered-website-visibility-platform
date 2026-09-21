"""Rate limiting, per host.

PER HOST, not per crawl. Two crawls of the same host share one budget, because
the host experiences the sum of what we send it and does not care how we have
organised ourselves internally.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class HostLimiter:
    """One concurrent request per host, with a delay between them.

    A semaphore alone would allow a burst; a delay alone would allow
    concurrency. A site that goes down because we audited it is not a site
    that renews.
    """

    default_delay: float = 1.0
    _delays: dict[str, float] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _next_allowed: dict[str, float] = field(default_factory=dict)

    def set_delay(self, host: str, seconds: float) -> None:
        self._delays[host] = seconds

    def delay_for(self, host: str) -> float:
        return self._delays.get(host, self.default_delay)

    def _lock(self, host: str) -> asyncio.Lock:
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        return self._locks[host]

    async def acquire(self, host: str) -> None:
        """Blocks until this host may be asked for another page."""
        await self._lock(host).acquire()
        wait = self._next_allowed.get(host, 0.0) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    def release(self, host: str) -> None:
        self._next_allowed[host] = time.monotonic() + self.delay_for(host)
        lock = self._locks.get(host)
        if lock and lock.locked():
            lock.release()

    def back_off(self, host: str, seconds: float) -> None:
        """A 429 or 503 means slow down, and stay slowed down."""
        self._delays[host] = max(self.delay_for(host), seconds)
        self._next_allowed[host] = time.monotonic() + seconds
