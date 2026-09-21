"""Database access.

TWO POOLS, because the request path and background work need opposite things
from row-level security.

  session()          connects as `app_user`. RLS APPLIES, and every request
                     runs in a transaction with `app.user_id` set. A handler
                     that forgets a tenant filter still cannot return another
                     organisation's rows.

  service_session()  connects as `app_service`, which has BYPASSRLS and is the
                     only role with access to the `secrets` schema. Used by
                     sync workers, which legitimately span organisations and
                     have no user to bind, and by the token vault.

Connecting the request path as the database owner or a superuser would bypass
every policy in migration 0006 and make them decorative. See db/roles.sql.

Service-role work must never be driven by a user-supplied filter: the caller
has already given up the backstop, so the tenant check has to be explicit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None
_service_pool: AsyncConnectionPool | None = None


def _build(dsn: str, min_size: int, max_size: int) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True},
    )


async def open_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
    service_dsn: str | None = None,
) -> None:
    global _pool, _service_pool
    if _pool is None:
        _pool = _build(dsn, min_size, max_size)
        await _pool.open(wait=True, timeout=10)
    if service_dsn and _service_pool is None:
        _service_pool = _build(service_dsn, min_size, max_size)
        await _service_pool.open(wait=True, timeout=10)


async def close_pool() -> None:
    global _pool, _service_pool
    for pool in (_pool, _service_pool):
        if pool is not None:
            await pool.close()
    _pool = _service_pool = None


def _require_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("database pool is not open")
    return _pool


def _require_service_pool() -> AsyncConnectionPool:
    if _service_pool is None:
        raise RuntimeError(
            "service database pool is not open; set SERVICE_DATABASE_URL"
        )
    return _service_pool


@asynccontextmanager
async def session(user_id: UUID | None = None) -> AsyncIterator[AsyncConnection]:
    """Yield a connection inside a transaction bound to one user.

    `SET LOCAL` is deliberate: the setting is scoped to the transaction, so a
    pooled connection can never leak one user's identity into the next
    request's queries.
    """
    async with _require_pool().connection() as conn:
        conn.row_factory = dict_row
        async with conn.transaction():
            if user_id is not None:
                await conn.execute(
                    "select set_config('app.user_id', %s, true)", (str(user_id),)
                )
            else:
                await conn.execute("select set_config('app.user_id', '', true)")
            yield conn


@asynccontextmanager
async def service_session() -> AsyncIterator[AsyncConnection]:
    """A connection for work that spans organisations, or that needs `secrets`.

    RLS does not apply here. Anything user-facing reached through this session
    must carry its own explicit tenant check — there is no backstop.
    """
    async with _require_service_pool().connection() as conn:
        conn.row_factory = dict_row
        async with conn.transaction():
            yield conn


@asynccontextmanager
async def service_task() -> AsyncIterator[AsyncConnection]:
    """A service connection for long background work, WITHOUT a wrapping
    transaction.

    `service_session` holds one transaction for its whole scope, which is
    right for a request and wrong for a job. Two reasons:

      A 500-page crawl inside one transaction is a transaction open for
      minutes, holding back vacuum and accumulating locks the whole time.

      And a failure rolls the whole thing back — including the row the job
      was about to write to say that it failed. A scheduler whose failure
      record disappears with the failure has no failure record.

    So every statement commits as it goes, and a job that dies halfway leaves
    both the work it managed and the note explaining why it stopped.
    """
    async with _require_service_pool().connection() as conn:
        conn.row_factory = dict_row
        yield conn


async def fetch_all(
    conn: AsyncConnection, sql: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


async def fetch_one(
    conn: AsyncConnection, sql: str, params: tuple[Any, ...] = ()
) -> dict[str, Any] | None:
    cur = await conn.execute(sql, params)
    return await cur.fetchone()
