"""Hub dependencies.

The Hub composes its own collaborators. It reuses the core's authentication and
database adapters — which is allowed, the dependency runs that way — but
nothing here reaches into crawler, analysis or scoring code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
import redis.asyncio as redis
from fastapi import Depends
from psycopg import AsyncConnection

from api.adapters import db
from api.config import GoogleSettings, Settings, get_settings
from api.hub.providers.google.client import DEFAULT_TIMEOUT, GoogleClient
from api.hub.services.keys import LocalKeyManager
from api.hub.services.oauth_state import RedisStateStore, StateStore
from api.hub.services.vault import TokenVault


def google_settings(
    settings: Annotated[Settings, Depends(get_settings)],
) -> GoogleSettings:
    return settings.google()


GoogleSettingsDep = Annotated[GoogleSettings, Depends(google_settings)]

_http_client: httpx.AsyncClient | None = None
_redis_client: redis.Redis | None = None


def http_client() -> httpx.AsyncClient:
    """One connection pool for the process. Google's endpoints are the same
    handful of hosts on every call, so per-request clients would throw away
    every TLS handshake."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    return _http_client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Tests inject a transport here rather than monkey-patching httpx."""
    global _http_client
    _http_client = client


async def close_clients() -> None:
    global _http_client, _redis_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


def google_client(settings: GoogleSettingsDep) -> GoogleClient:
    return GoogleClient(
        settings.client_id, settings.client_secret, http=http_client()
    )


GoogleClientDep = Annotated[GoogleClient, Depends(google_client)]

_state_store_override: StateStore | None = None


def set_state_store(store: StateStore | None) -> None:
    global _state_store_override
    _state_store_override = store


def state_store(settings: GoogleSettingsDep) -> StateStore:
    global _redis_client
    if _state_store_override is not None:
        return _state_store_override
    if _redis_client is None:
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    return RedisStateStore(_redis_client)


StateStoreDep = Annotated[StateStore, Depends(state_store)]


async def service_connection() -> AsyncIterator[AsyncConnection]:
    """The Hub writes connections, properties and secrets as the service role.

    RLS does not apply here, so every statement the Hub runs on this connection
    carries its own explicit organisation check. The organisation is taken from
    the session or from server-side OAuth state, never from a request body.
    """
    async with db.service_session() as conn:
        yield conn


ServiceConnectionDep = Annotated[AsyncConnection, Depends(service_connection)]


def token_vault(
    conn: ServiceConnectionDep, settings: GoogleSettingsDep
) -> TokenVault:
    return TokenVault(conn, LocalKeyManager.from_base64(settings.token_master_key))


TokenVaultDep = Annotated[TokenVault, Depends(token_vault)]
