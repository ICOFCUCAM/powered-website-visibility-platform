"""Database access.

Two things make this more than a connection pool:

1. Every request runs inside a transaction that has `app.user_id` set, so the
   row-level security policies in migration 0006 apply. Handlers get defence in
   depth: if one forgets a tenant filter, RLS still refuses the rows.

2. The pool connects as `app_user` (see db/roles.sql), never as a superuser or
   the table owner. RLS is bypassed entirely by superusers, so connecting as
   one would make every policy decorative.
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


async def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
    global _pool
    if _pool is not None:
        return
    _pool = AsyncConnectionPool(
        dsn, min_size=min_size, max_size=max_size, open=False, kwargs={"autocommit": True}
    )
    await _pool.open(wait=True, timeout=10)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _require_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("database pool is not open")
    return _pool


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
