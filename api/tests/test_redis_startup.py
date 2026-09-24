"""Redis is checked when the app starts, not when a customer needs it.

`REDIS_URL` is required at startup but every client that reads it connects
lazily, so a wrong value produces a deployment that comes up green and fails
later — at the first peek rate limit and the first Google connect. These tests
are about where that failure happens, which is the whole point of the check.
"""

from __future__ import annotations

import pytest

from api.adapters import cache
from api.config import ConfigError


async def test_an_unreachable_redis_stops_the_app_from_starting():
    """Port 1 is reserved and nothing listens on it, so this is a connection
    refused rather than a timeout — the fast case, and the one a typo in the
    hostname or port actually produces."""
    with pytest.raises(ConfigError) as caught:
        await cache.check_reachable("redis://127.0.0.1:1/0")

    message = str(caught.value)
    assert "REDIS_URL" in message, "the message has to name the variable to fix"
    # Whoever reads this is looking at a failed deploy with no other clue.
    assert "broker" in message


async def test_a_redis_that_answers_is_not_an_error(monkeypatch):
    """The check must not fail open *or* fail closed on a healthy broker: a
    startup probe that spuriously refuses is worse than no probe, because it
    takes down a deployment that would have worked."""

    class Answering:
        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("api.adapters.cache.redis.from_url", lambda *a, **k: Answering())
    await cache.check_reachable("redis://wherever:6379/0")


async def test_the_client_is_closed_even_when_the_ping_fails(monkeypatch):
    """A socket left open on every failed start is a file descriptor leak in
    exactly the situation — a crash loop — where it accumulates fastest."""
    closed: list[bool] = []

    class Refusing:
        async def ping(self) -> bool:
            raise ConnectionError("nope")

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr("api.adapters.cache.redis.from_url", lambda *a, **k: Refusing())
    with pytest.raises(ConfigError):
        await cache.check_reachable("redis://wherever:6379/0")
    assert closed == [True]
