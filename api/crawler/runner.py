"""Running a crawl.

Seed from robots and sitemaps, lease from the frontier, fetch politely,
extract, store the raw artifact, queue what was linked, stop at the cap.

Two rules govern when it runs at all:

  - crawl_allowed is re-evaluated HERE, not trusted from admission. Between a
    job being queued and a worker picking it up, verification can be revoked,
    a plan can lapse or a site can be suspended. A queue entry must never
    outlive the permission that created it (locked decision 20).

  - Hitting the page cap is recorded and surfaced. A truncated crawl that
    looks complete produces analysis that is quietly wrong.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from psycopg import AsyncConnection

from api.adapters.db import fetch_one
from api.crawler.extract import PageFacts, extract
from api.crawler.fetch import Fetcher, FetchResult
from api.crawler.frontier import Frontier
from api.crawler.politeness import HostLimiter
from api.crawler.render import DEFAULT_RENDER_BUDGET, NoRenderer, Renderer, should_render
from api.crawler.robots import RobotsPolicy, blocked_ai_crawlers, parse_robots
from api.crawler.sitemaps import (
    CONVENTIONAL_PATHS,
    MAX_SITEMAP_FILES,
    MAX_URLS,
    decode,
    extract_locations,
    is_index,
)
from api.crawler.storage import ArtifactStore, artifact_key
from api.crawler.urls import ScopeRule, looks_like_a_trap, normalise, url_hash

logger = logging.getLogger("visibility_hub.crawler")


@dataclass
class CrawlSummary:
    discovered: int = 0
    fetched: int = 0
    rendered: int = 0
    skipped: int = 0
    errors: int = 0
    error_kinds: dict[str, int] = field(default_factory=dict)
    hit_page_cap: bool = False
    robots_blocked: bool = False
    robots_missing: bool = False
    crawl_delay_capped: bool = False
    ai_crawlers_blocked: list[str] = field(default_factory=list)
    sitemaps_found: list[str] = field(default_factory=list)
    stopped_reason: str | None = None

    def record_error(self, kind: str) -> None:
        self.errors += 1
        self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1


class CrawlRunner:
    def __init__(
        self,
        conn: AsyncConnection,
        *,
        fetcher: Fetcher | None = None,
        store: ArtifactStore,
        renderer: Renderer | None = None,
        limiter: HostLimiter | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._conn = conn
        self._limiter = limiter or HostLimiter()
        self._fetcher = fetcher or Fetcher(self._limiter)
        self._store = store
        self._renderer = renderer or NoRenderer()
        self._worker_id = worker_id or f"worker-{uuid4().hex[:8]}"

    async def run(
        self,
        *,
        crawl_id: UUID,
        organization_id: UUID,
        website_id: UUID,
        origin: str,
        max_pages: int,
        include_subdomains: bool = False,
    ) -> CrawlSummary:
        summary = CrawlSummary()

        # THE SECOND GATE (locked decision 20). Admission checked this when the
        # job was queued; between then and now verification can be revoked, a
        # plan can lapse or a website can be suspended. A queue entry must
        # never outlive the permission that created it.
        refusal = await self._permission_refusal(website_id)
        if refusal is not None:
            summary.stopped_reason = refusal
            await self._finish(crawl_id, summary, status="cancelled")
            return summary

        frontier = Frontier(self._conn, crawl_id)
        scope = ScopeRule(
            host=(urlsplit(origin).hostname or "").lower(),
            include_subdomains=include_subdomains,
        )
        render_budget = DEFAULT_RENDER_BUDGET

        await self._conn.execute(
            "update crawls set status = 'running', started_at = now(), "
            "       heartbeat_at = now() where id = %s",
            (crawl_id,),
        )

        robots = await self._seed(crawl_id, origin, scope, frontier, summary)
        if robots.blocks_everything:
            # A finding for the customer, not a crawler failure: their site is
            # telling every crawler, including Google's, to stay out.
            summary.robots_blocked = True
            summary.stopped_reason = "robots_disallows_crawl"
            await self._finish(crawl_id, summary, status="completed")
            return summary

        host = scope.host
        self._limiter.set_delay(host, robots.delay_seconds)

        while summary.fetched < max_pages:
            await frontier.reclaim_expired()
            batch = await frontier.lease(self._worker_id, limit=5)
            if not batch:
                if await frontier.is_drained():
                    break
                continue

            for item in batch:
                if summary.fetched >= max_pages:
                    # Leave the rest pending rather than marking them done:
                    # the frontier then honestly shows what was not reached.
                    summary.hit_page_cap = True
                    summary.stopped_reason = "page_cap_reached"
                    break

                if not robots.allows(item.url):
                    await frontier.skip(item.url_hash, "robots_disallow")
                    summary.skipped += 1
                    continue

                result = await self._fetcher.fetch(item.url)
                if not result.ok:
                    summary.record_error(result.error or "unknown")
                    await frontier.fail(
                        item.url_hash, result.error or "unknown", retry=True
                    )
                    continue

                facts, rendered = await self._maybe_render(result, render_budget)
                if rendered:
                    render_budget -= 1
                    summary.rendered += 1

                await self._persist(
                    crawl_id=crawl_id,
                    organization_id=organization_id,
                    website_id=website_id,
                    item_url=item.url,
                    item_hash=item.url_hash,
                    depth=item.depth,
                    result=result,
                    facts=facts,
                    rendered=rendered,
                )
                summary.fetched += 1
                await frontier.complete(item.url_hash)

                if facts is not None:
                    summary.discovered += await self._queue_links(
                        frontier, facts, scope, item.depth
                    )

            await self._conn.execute(
                "update crawls set heartbeat_at = now(), pages_fetched = %s, "
                "       pages_rendered = %s where id = %s",
                (summary.fetched, summary.rendered, crawl_id),
            )

        await self._finish(crawl_id, summary, status="completed")
        return summary

    async def _permission_refusal(self, website_id: UUID) -> str | None:
        """Re-evaluates the crawl prerequisites from current state.

        Reads `website_ownership_evidence` rather than `websites.crawl_allowed`:
        that column is derived and kept for observability, never the authority.
        """
        row = await fetch_one(
            self._conn,
            """
            select w.status, w.ownership_verified_at,
                   coalesce(
                       (select bool_or(is_sufficient_evidence)
                          from website_ownership_evidence e
                         where e.website_id = w.id), false) as covers
              from websites w
             where w.id = %s and w.archived_at is null
            """,
            (website_id,),
        )
        if row is None:
            return "website_gone"
        if row["ownership_verified_at"] is None:
            return "ownership_not_verified"
        if not row["covers"]:
            return "ownership_does_not_cover_target"
        if row["status"] not in ("PENDING", "CONNECTING", "READY", "ERROR"):
            return f"website_status_{str(row['status']).lower()}"
        return None

    # -- seeding -----------------------------------------------------------

    async def _seed(
        self,
        crawl_id: UUID,
        origin: str,
        scope: ScopeRule,
        frontier: Frontier,
        summary: CrawlSummary,
    ) -> RobotsPolicy:
        robots_result = await self._fetcher.fetch(origin.rstrip("/") + "/robots.txt")
        body = robots_result.body if robots_result.ok else ""
        robots = parse_robots(body, robots_result.status_code or 599, origin)

        summary.robots_missing = not robots.fetched
        summary.crawl_delay_capped = robots.capped_delay
        summary.ai_crawlers_blocked = blocked_ai_crawlers(body)

        key = None
        if body:
            key = await self._store.put(f"{crawl_id}/robots.txt.gz", body.encode())

        candidates = list(robots.sitemaps) or [
            origin.rstrip("/") + path for path in CONVENTIONAL_PATHS
        ]
        seeded = await self._read_sitemaps(candidates, scope, frontier, summary)
        summary.sitemaps_found = [s for s in candidates if s]

        # The homepage is always a seed: a site with no sitemap still has one.
        await frontier.add([(origin.rstrip("/") + "/", 0)])
        summary.discovered += seeded + 1

        await self._conn.execute(
            "update crawls set robots_key = %s, sitemap_urls = %s where id = %s",
            (key, summary.sitemaps_found[:20], crawl_id),
        )
        return robots

    async def _read_sitemaps(
        self,
        candidates: list[str],
        scope: ScopeRule,
        frontier: Frontier,
        summary: CrawlSummary,
    ) -> int:
        queued = 0
        seen: set[str] = set()
        queue = list(candidates[:MAX_SITEMAP_FILES])

        while queue and len(seen) < MAX_SITEMAP_FILES and queued < MAX_URLS:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)

            result = await self._fetcher.fetch(url)
            if not result.ok or (result.status_code or 0) >= 400:
                continue

            text = decode(result.raw or result.body.encode(), url)
            locations = extract_locations(text)

            if is_index(text):
                queue.extend(locations)
                continue

            batch = []
            for raw in locations:
                candidate = normalise(raw)
                if not candidate or not scope.covers(candidate):
                    continue
                if looks_like_a_trap(candidate):
                    continue
                batch.append((candidate, 0))
            queued += await frontier.add(batch)

        return queued

    # -- per page ----------------------------------------------------------

    async def _maybe_render(
        self, result: FetchResult, budget: int
    ) -> tuple[PageFacts | None, bool]:
        if not result.is_html or not result.body:
            return None, False

        facts = extract(result.body, url=result.url)
        decision = should_render(facts, budget_remaining=budget)
        if not decision.render:
            return facts, False

        html = await self._renderer.render(result.url)
        if html is None:
            # No renderer configured. The snapshot keeps the unrendered facts
            # and records why, rather than pretending a shell was the page.
            return facts, False
        return extract(html, url=result.url), True

    async def _queue_links(
        self, frontier: Frontier, facts: PageFacts, scope: ScopeRule, depth: int
    ) -> int:
        batch: list[tuple[str, int]] = []
        for link in facts.internal_links:
            candidate = normalise(link.url)
            if not candidate or not scope.covers(candidate):
                continue
            if looks_like_a_trap(candidate):
                continue
            batch.append((candidate, depth + 1))
        return await frontier.add(batch)

    async def _persist(
        self,
        *,
        crawl_id: UUID,
        organization_id: UUID,
        website_id: UUID,
        item_url: str,
        item_hash: bytes,
        depth: int,
        result: FetchResult,
        facts: PageFacts | None,
        rendered: bool,
    ) -> None:
        page = await fetch_one(
            self._conn,
            """
            insert into pages (organization_id, website_id, url, url_hash, path,
                               last_status, last_crawl_id, is_indexable)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (website_id, url_hash) do update
               set last_seen_at = now(),
                   last_status = excluded.last_status,
                   last_crawl_id = excluded.last_crawl_id,
                   is_indexable = excluded.is_indexable,
                   gone_at = null
            returning id
            """,
            (
                organization_id,
                website_id,
                item_url,
                item_hash,
                urlsplit(item_url).path or "/",
                result.status_code,
                crawl_id,
                _is_indexable(facts),
            ),
        )
        assert page is not None
        page_id = page["id"]

        raw_key = None
        if result.raw:
            raw_key = await self._store.put(
                artifact_key(website_id, crawl_id, item_hash), result.raw
            )

        f = facts or PageFacts()
        await self._conn.execute(
            """
            insert into page_snapshots (
                organization_id, website_id, page_id, crawl_id, fetched_at,
                status_code, content_type, render_mode, response_time_ms, bytes,
                redirect_chain, content_hash, raw_key, title, title_len,
                meta_description, meta_description_len, h1, heading_counts,
                word_count, text_hash, lang, canonical_url, canonical_is_self,
                robots_meta, hreflang, viewport_present, schema_types,
                schema_errors, open_graph, depth, internal_outlinks,
                external_outlinks, images_total, images_missing_alt,
                extract_version
            ) values (
                %s, %s, %s, %s, now(),
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                'v1'
            )
            """,
            (
                organization_id, website_id, page_id, crawl_id,
                result.status_code, result.content_type,
                "browser" if rendered else "http",
                result.elapsed_ms, len(result.raw),
                _json(result.redirect_chain), _content_hash(result.raw), raw_key,
                f.title, f.title_length,
                f.meta_description,
                len(f.meta_description) if f.meta_description else None,
                f.h1, _json(f.heading_counts),
                f.word_count, f.text_hash, f.lang, f.canonical_url,
                _canonical_is_self(f, item_url),
                f.robots_meta, _json(f.hreflang), f.viewport_present,
                list(dict.fromkeys(f.schema_types)), _json(f.schema_errors),
                _json(f.open_graph), depth,
                len(f.internal_links), len(f.external_links),
                f.images_total, f.images_missing_alt,
            ),
        )

        if facts is not None:
            await self._persist_links(crawl_id, website_id, page_id, facts)

    async def _persist_links(
        self, crawl_id: UUID, website_id: UUID, page_id: UUID, facts: PageFacts
    ) -> None:
        rows = [
            (
                crawl_id, website_id, page_id, url_hash(link.url), link.url,
                link.anchor_text or None, link.rel, link.is_internal,
                website_id,
            )
            for link in facts.links
        ]
        if not rows:
            return
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                insert into page_links (crawl_id, website_id, organization_id,
                                        from_page_id, to_url_hash, to_url,
                                        anchor_text, rel, is_internal)
                select %s, %s, w.organization_id, %s, %s, %s, %s, %s, %s
                  from websites w where w.id = %s
                on conflict (crawl_id, from_page_id, to_url_hash) do nothing
                """,
                rows,
            )

    async def _finish(
        self, crawl_id: UUID, summary: CrawlSummary, *, status: str
    ) -> None:
        await self._conn.execute(
            """
            update crawls
               set status = %s, finished_at = now(),
                   pages_discovered = %s, pages_fetched = %s,
                   pages_rendered = %s, fetch_errors = %s,
                   error_summary = %s
             where id = %s
            """,
            (
                status,
                summary.discovered,
                summary.fetched,
                summary.rendered,
                summary.errors,
                _json(
                    {
                        "kinds": summary.error_kinds,
                        "skipped": summary.skipped,
                        "hit_page_cap": summary.hit_page_cap,
                        "robots_blocked": summary.robots_blocked,
                        "robots_missing": summary.robots_missing,
                        "crawl_delay_capped": summary.crawl_delay_capped,
                        "ai_crawlers_blocked": summary.ai_crawlers_blocked,
                        "stopped_reason": summary.stopped_reason,
                    }
                ),
                crawl_id,
            ),
        )


def _json(value) -> str:
    import json

    return json.dumps(value, default=str)


def _content_hash(raw: bytes) -> bytes | None:
    import hashlib

    return hashlib.sha256(raw).digest() if raw else None


def _is_indexable(facts: PageFacts | None) -> bool | None:
    if facts is None:
        return None
    return "noindex" not in facts.robots_meta


def _canonical_is_self(facts: PageFacts, url: str) -> bool | None:
    if not facts.canonical_url:
        return None
    return normalise(facts.canonical_url) == normalise(url)
