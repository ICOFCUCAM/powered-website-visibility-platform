"""Temporary OAuth state (V1 spec s4: Redis).

Why server-side rather than a signed cookie or a self-describing `state`:
PKCE's guarantee is that an attacker who intercepts the authorization code
cannot exchange it, because they lack the verifier. Putting the verifier into
the `state` parameter sends it down the same channel as the code and throws
that guarantee away. So `state` is an opaque random id and everything else
lives in Redis, keyed by it.

Two properties the store must have:
  - short TTL, so a stale authorization cannot be completed much later
  - single use, so a replayed callback cannot bind a second connection
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID

import redis.asyncio as redis

STATE_TTL_SECONDS = 600
_PREFIX = "oauth:state:"


@dataclass(frozen=True, slots=True)
class OAuthState:
    organization_id: str
    user_id: str
    website_id: str | None
    code_verifier: str
    services: list[str]
    redirect_after: str

    def as_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> OAuthState:
        return cls(**json.loads(raw))


def new_state_id() -> str:
    # 256 bits: the id is the only thing standing between a callback and a
    # pending authorization, so it is not a UUID.
    return secrets.token_urlsafe(32)


class StateStore(Protocol):
    async def put(self, state_id: str, state: OAuthState) -> None: ...

    async def take(self, state_id: str) -> OAuthState | None: ...


class RedisStateStore:
    def __init__(self, client: redis.Redis, ttl: int = STATE_TTL_SECONDS) -> None:
        self._redis = client
        self._ttl = ttl

    async def put(self, state_id: str, state: OAuthState) -> None:
        await self._redis.set(_PREFIX + state_id, state.as_json(), ex=self._ttl)

    async def take(self, state_id: str) -> OAuthState | None:
        """Atomically read and delete: a callback can be completed exactly once.

        GETDEL rather than GET-then-DEL, so two concurrent callbacks cannot both
        see the state and both create a connection.
        """
        raw = await self._redis.getdel(_PREFIX + state_id)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return OAuthState.from_json(raw)


class InMemoryStateStore:
    """For tests only. Not safe across processes, which is why it is not the
    default anywhere."""

    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    async def put(self, state_id: str, state: OAuthState) -> None:
        self._items[state_id] = state.as_json()

    async def take(self, state_id: str) -> OAuthState | None:
        raw = self._items.pop(state_id, None)
        return OAuthState.from_json(raw) if raw else None


def uuid_or_none(value: str | None) -> UUID | None:
    return UUID(value) if value else None
