"""The connection pool, opened once at startup.

One pool per process, opened in the application's lifespan and closed with it.
The API and the worker are separate processes with separate pools, which is
why the pool sizes are configuration rather than constants: the API wants many
short connections, the worker wants two long ones.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


async def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
    global _pool
    if _pool is not None:
        return
    _pool = AsyncConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=False,
    )
    await _pool.open(wait=True)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool is not open")
    return _pool


@asynccontextmanager
async def connection() -> AsyncIterator[Any]:
    async with pool().connection() as conn:
        yield conn


@asynccontextmanager
async def transaction() -> AsyncIterator[Any]:
    """A connection with an explicit transaction around it.

    The pool runs in autocommit, so every statement is its own transaction
    unless something asks otherwise. Promotion asks: moving the production
    pointer and marking the previous deployment must not be separable, or a
    crash between them leaves a project pointing at a deployment it has
    already superseded.
    """
    async with pool().connection() as conn, conn.transaction():
        yield conn
