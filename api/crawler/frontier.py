"""The crawl frontier.

A Postgres table leased with SELECT ... FOR UPDATE SKIP LOCKED, because of the
recovery invariant (locked decision, docs/12):

    A URL whose lease expires becomes eligible for another worker WITHOUT
    requiring the original worker to recover.

worker_id is recorded for diagnostics and is NEVER consulted when reclaiming a
lease — consulting it would make recovery depend on knowing something about
the dead worker, which is precisely what this design avoids.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one
from api.crawler.urls import url_hash

LEASE_SECONDS = 300


@dataclass(frozen=True, slots=True)
class FrontierItem:
    url: str
    url_hash: bytes
    depth: int


class Frontier:
    def __init__(self, conn: AsyncConnection, crawl_id: UUID) -> None:
        self._conn = conn
        self._crawl_id = crawl_id

    async def add(
        self, urls: list[tuple[str, int]], *, discovered_from: bytes | None = None
    ) -> int:
        """Idempotent: a URL linked from twenty pages is queued once."""
        if not urls:
            return 0
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                insert into crawl_frontier
                    (crawl_id, url_hash, url, depth, discovered_from)
                values (%s, %s, %s, %s, %s)
                on conflict (crawl_id, url_hash) do nothing
                """,
                [
                    (self._crawl_id, url_hash(url), url, depth, discovered_from)
                    for url, depth in urls
                ],
            )
        return len(urls)

    async def lease(self, worker_id: str, limit: int = 10) -> list[FrontierItem]:
        rows = await fetch_all(
            self._conn,
            """
            update crawl_frontier f
               set state = 'leased',
                   worker_id = %s,
                   leased_at = now(),
                   lease_expires_at = now() + make_interval(secs => %s),
                   attempts = attempts + 1
             where (f.crawl_id, f.url_hash) in (
                   select crawl_id, url_hash from crawl_frontier
                    where crawl_id = %s and state = 'pending'
                    order by depth, url_hash
                    limit %s
                      for update skip locked)
            returning f.url, f.url_hash, f.depth
            """,
            (worker_id, LEASE_SECONDS, self._crawl_id, limit),
        )
        return [
            FrontierItem(url=r["url"], url_hash=bytes(r["url_hash"]), depth=r["depth"])
            for r in rows
        ]

    async def complete(self, hash_: bytes) -> None:
        await self._conn.execute(
            "update crawl_frontier set state = 'done', worker_id = null "
            " where crawl_id = %s and url_hash = %s",
            (self._crawl_id, hash_),
        )

    async def fail(self, hash_: bytes, error: str, *, retry: bool) -> None:
        """Three attempts, then give up on this URL rather than the crawl."""
        await self._conn.execute(
            """
            update crawl_frontier
               set state = case when %s and attempts < 3 then 'pending' else 'failed' end,
                   worker_id = null,
                   lease_expires_at = null,
                   last_error = %s
             where crawl_id = %s and url_hash = %s
            """,
            (retry, error[:500], self._crawl_id, hash_),
        )

    async def skip(self, hash_: bytes, reason: str) -> None:
        await self._conn.execute(
            "update crawl_frontier set state = 'skipped', skip_reason = %s, "
            "       worker_id = null "
            " where crawl_id = %s and url_hash = %s",
            (reason, self._crawl_id, hash_),
        )

    async def reclaim_expired(self) -> int:
        """The recovery invariant, made concrete.

        Note what this query does not do: look at worker_id. An expired lease
        is reclaimable regardless of who held it or whether that process still
        exists.
        """
        row = await fetch_one(
            self._conn,
            """
            with reclaimed as (
                update crawl_frontier
                   set state = 'pending', worker_id = null, lease_expires_at = null
                 where crawl_id = %s and state = 'leased'
                   and lease_expires_at < now()
                returning 1)
            select count(*) as n from reclaimed
            """,
            (self._crawl_id,),
        )
        return int(row["n"]) if row else 0

    async def counts(self) -> dict[str, int]:
        rows = await fetch_all(
            self._conn,
            "select state, count(*) as n from crawl_frontier "
            " where crawl_id = %s group by state",
            (self._crawl_id,),
        )
        return {r["state"]: int(r["n"]) for r in rows}

    async def is_drained(self) -> bool:
        counts = await self.counts()
        return not (counts.get("pending") or counts.get("leased"))
