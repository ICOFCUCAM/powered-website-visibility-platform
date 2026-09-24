"""Redis, and whether it is actually there.

Every client in this codebase creates its connection lazily, on the first
call that needs one. That is right for a request path — it keeps startup off
the critical path of a deploy — and it is exactly what makes a misconfigured
broker so hard to see: the process starts, the health check passes, and the
first sign of trouble is a customer's failed action.

This module is the one place that connects eagerly, so the failure lands at
startup instead.
"""

from __future__ import annotations

import asyncio

import redis.asyncio as redis

from api.config import ConfigError

#: Long enough for a container on the same host to answer, short enough that a
#: hostname resolving to something that silently drops packets still fails the
#: deploy rather than hanging it until the platform's build timeout.
PING_TIMEOUT_SECONDS = 5


async def check_reachable(url: str) -> None:
    """One round trip to Redis, raising `ConfigError` if it does not answer.

    The API needs Redis for peek rate limits (`api/peek/limits.py`) and for
    OAuth state (`api/hub/services/oauth_state.py`); the workers need it as
    the Celery broker. None of those can degrade gracefully, so a deployment
    that cannot reach it is not a working deployment and should not take over
    from one that is.
    """
    client = redis.from_url(url, decode_responses=True)
    try:
        async with asyncio.timeout(PING_TIMEOUT_SECONDS):
            await client.ping()
    except Exception as exc:
        raise ConfigError(
            f"REDIS_URL is set but Redis did not answer: {exc!r}. "
            "The API needs it for peek rate limits and Google OAuth state, "
            "and the workers use it as the Celery broker."
        ) from exc
    finally:
        await client.aclose()
