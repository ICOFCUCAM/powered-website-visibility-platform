"""How often a stranger may ask.

The scan endpoint has no account behind it, so the only thing bounding what
it costs us is this. Two limits, because they stop different things:

  PER VISITOR, so one person cannot sit on the button. Keyed by IP, which is
  imperfect — a university shares one — but the limit is generous enough that
  a human never meets it and a script meets it immediately.

  GLOBAL, because the per-visitor limit does nothing against a thousand IPs.
  This is the one that decides the worst case: whatever it is set to is the
  most outbound fetching this endpoint can ever do in a minute, no matter who
  is asking or how many of them there are.

Fails CLOSED. If Redis is unreachable we cannot count, and an endpoint that
fetches arbitrary URLs is not something to leave uncounted.
"""

from __future__ import annotations

import logging

import redis.asyncio as redis

logger = logging.getLogger("visibility_hub.peek")

#: Scans per visitor per window. A person checking their site, then a typo,
#: then their client's site, never reaches this.
PER_IP = 5
PER_IP_WINDOW = 300

#: Scans for everybody, per minute. The ceiling on what this can cost.
GLOBAL = 60
GLOBAL_WINDOW = 60


class TooMany(Exception):
    """Told to the visitor in plain words, with no numbers they could probe."""


async def claim(client: redis.Redis | None, ip: str) -> None:
    """Take one scan's worth of allowance, or raise `TooMany`."""
    if client is None:
        # No counter means no limit, and no limit on an endpoint that fetches
        # what it is told to fetch is not a state to serve traffic in.
        raise TooMany("Scanning is unavailable right now.")

    try:
        for key, limit, window in (
            (f"peek:ip:{ip}", PER_IP, PER_IP_WINDOW),
            ("peek:all", GLOBAL, GLOBAL_WINDOW),
        ):
            used = await client.incr(key)
            if used == 1:
                await client.expire(key, window)
            if used > limit:
                raise TooMany(
                    "That's a lot of scans. Try again in a few minutes."
                )
    except redis.RedisError as exc:
        logger.warning("peek limiter unavailable: %s", exc)
        raise TooMany("Scanning is unavailable right now.") from exc
