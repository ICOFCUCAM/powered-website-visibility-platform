"""The frontier and the recovery invariant."""

from __future__ import annotations

import asyncio
import time

from api.crawler.politeness import HostLimiter


async def test_one_request_at_a_time_per_host():
    """Two overlapping crawls of the same host share one budget, because the
    host experiences the sum of what we send it."""
    limiter = HostLimiter(default_delay=0.05)
    order: list[str] = []

    async def visit(tag: str) -> None:
        await limiter.acquire("example.com")
        order.append(f"{tag}:start")
        await asyncio.sleep(0.01)
        order.append(f"{tag}:end")
        limiter.release("example.com")

    await asyncio.gather(visit("a"), visit("b"))
    # Never interleaved: each request finishes before the next begins.
    assert order in (
        ["a:start", "a:end", "b:start", "b:end"],
        ["b:start", "b:end", "a:start", "a:end"],
    )


async def test_the_delay_is_actually_waited():
    limiter = HostLimiter(default_delay=0.15)
    started = time.monotonic()

    for _ in range(3):
        await limiter.acquire("example.com")
        limiter.release("example.com")

    # Three acquisitions means two gaps of 0.15s.
    assert time.monotonic() - started >= 0.28


async def test_different_hosts_do_not_block_each_other():
    limiter = HostLimiter(default_delay=0.2)
    started = time.monotonic()

    async def visit(host: str) -> None:
        await limiter.acquire(host)
        limiter.release(host)

    await asyncio.gather(visit("a.test"), visit("b.test"), visit("c.test"))
    assert time.monotonic() - started < 0.1


async def test_backing_off_slows_that_host_and_stays_slowed():
    """A 429 means slow down. Returning to the old rate on the next request
    is how a temporary block becomes a permanent one."""
    limiter = HostLimiter(default_delay=1.0)
    assert limiter.delay_for("example.com") == 1.0

    limiter.back_off("example.com", 10.0)
    assert limiter.delay_for("example.com") == 10.0
    # Other hosts are unaffected.
    assert limiter.delay_for("other.test") == 1.0


def test_a_robots_crawl_delay_overrides_the_default():
    limiter = HostLimiter(default_delay=1.0)
    limiter.set_delay("slow.test", 5.0)
    assert limiter.delay_for("slow.test") == 5.0
    assert limiter.delay_for("fast.test") == 1.0
