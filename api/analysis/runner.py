"""Running the analysis.

Assemble the context, run every rule, reconcile the findings against what was
already known, write an observation for each, and compute the score.

THE RECONCILIATION IS THE PRODUCT. A finding that is still present updates an
existing issue rather than creating a new one; a finding that has gone marks
its issue resolved; a resolved issue that comes back is marked regressed. That
lifecycle is only possible because fingerprints are deterministic, and it is
what lets the platform say "you fixed this and it held" rather than handing
over a fresh list of near-duplicate advice every week.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one
from api.analysis.catalogue import BY_KEY
from api.analysis.ctr_baseline import build_curve
from api.analysis.rules import ai_visibility, content, search, technical  # noqa: F401
from api.analysis.rules.base import (
    AnalysisContext,
    Finding,
    PagePerformance,
    PageRow,
    QueryRow,
    all_rules,
)
from api.analysis.scoring import (
    CATEGORY_TO_COMPONENT,
    SCORING_VERSION,
    Component,
    score_components,
)

logger = logging.getLogger("visibility_hub.analysis")


class NoCrawlToAnalyse(RuntimeError):
    """There is nothing to reconcile against. See `AnalysisRunner.run`."""

ANALYSIS_WINDOW_DAYS = 28
CURVE_WINDOW_DAYS = 90
GSC_LAG_DAYS = 3


@dataclass
class AnalysisResult:
    issues_open: int = 0
    issues_new: int = 0
    issues_resolved: int = 0
    issues_regressed: int = 0
    score_total: float | None = None
    pages_evaluated: int = 0
    rules_run: int = 0
    findings: list[Finding] = field(default_factory=list)


class AnalysisRunner:
    def __init__(self, conn: AsyncConnection, *, today: date | None = None) -> None:
        self._conn = conn
        self._today = today or date.today()

    async def run(
        self, *, organization_id: UUID, website_id: UUID, crawl_id: UUID | None
    ) -> AnalysisResult:
        """`crawl_id=None` means THE LATEST COMPLETED CRAWL, not "no crawl".

        The distinction is not pedantic. With no pages in the context every
        crawl-derived rule finds nothing, and reconciliation reads "nothing
        found" as "everything fixed" — so a nightly re-score that passed None
        would quietly mark a customer's whole audit resolved. It is the sort
        of bug that looks like good news on the dashboard.

        A website with no completed crawl raises rather than analysing an
        empty page set, because there is no honest score to produce from one.
        """
        crawl_id = crawl_id or await self._latest_crawl(website_id)
        if crawl_id is None:
            raise NoCrawlToAnalyse(
                f"website {website_id} has no completed crawl to analyse"
            )
        ctx = await self._context(website_id, crawl_id)
        findings = self._evaluate(ctx)
        result = await self._reconcile(
            organization_id, website_id, crawl_id, ctx, findings
        )
        await self._score(organization_id, website_id, crawl_id, ctx, result)
        return result

    # -- context -----------------------------------------------------------

    async def _latest_crawl(self, website_id: UUID) -> UUID | None:
        row = await fetch_one(
            self._conn,
            "select id from crawls where website_id = %s and status = 'completed'"
            " order by finished_at desc nulls last limit 1",
            (website_id,),
        )
        return row["id"] if row else None

    async def _context(self, website_id: UUID, crawl_id: UUID | None) -> AnalysisContext:
        site = await fetch_one(
            self._conn,
            "select canonical_url from websites where id = %s", (website_id,)
        )
        ctx = AnalysisContext(
            website_id=website_id, origin=(site or {}).get("canonical_url", "")
        )

        end = self._today - timedelta(days=GSC_LAG_DAYS)
        start = end - timedelta(days=ANALYSIS_WINDOW_DAYS - 1)
        ctx.window_start, ctx.window_end = start, end

        if crawl_id is not None:
            ctx.pages = await self._pages(website_id, crawl_id)
            ctx.linked_url_hashes = await self._linked(crawl_id)
            ctx.broken_link_targets = await self._broken_links(crawl_id)
            crawl = await fetch_one(
                self._conn,
                "select sitemap_urls, error_summary from crawls where id = %s",
                (crawl_id,),
            )
            if crawl:
                summary = crawl["error_summary"] or {}
                ctx.ai_crawlers_blocked = summary.get("ai_crawlers_blocked") or []
                ctx.robots_blocks_crawl = bool(summary.get("robots_blocked"))
                ctx.sitemap_found = bool(crawl["sitemap_urls"])

        ctx.queries = await self._queries(website_id, start, end)
        ctx.page_performance = await self._page_performance(website_id, start, end)

        prior_end = start - timedelta(days=1)
        prior_start = prior_end - timedelta(days=ANALYSIS_WINDOW_DAYS - 1)
        ctx.prior_page_performance = await self._page_performance(
            website_id, prior_start, prior_end
        )

        if ctx.has_search_data:
            ctx.ctr_curve = await build_curve(
                self._conn, website_id,
                end - timedelta(days=CURVE_WINDOW_DAYS), end,
            )
        return ctx

    async def _pages(self, website_id: UUID, crawl_id: UUID) -> list[PageRow]:
        rows = await fetch_all(
            self._conn,
            """
            select p.id as page_id, p.url, p.url_hash,
                   s.status_code, s.title, s.meta_description, s.h1,
                   s.word_count, s.canonical_url, s.canonical_is_self,
                   s.robots_meta, s.viewport_present, s.schema_types,
                   s.images_total, s.images_missing_alt, s.internal_outlinks,
                   s.text_hash, s.redirect_chain, s.depth, s.render_mode
              from page_snapshots s
              join pages p on p.id = s.page_id
             where s.crawl_id = %s and s.website_id = %s
            """,
            (crawl_id, website_id),
        )
        return [
            PageRow(
                page_id=r["page_id"], url=r["url"], url_hash=bytes(r["url_hash"]),
                status_code=r["status_code"], title=r["title"],
                meta_description=r["meta_description"], h1=r["h1"] or [],
                word_count=r["word_count"] or 0, canonical_url=r["canonical_url"],
                canonical_is_self=r["canonical_is_self"],
                robots_meta=r["robots_meta"] or [],
                viewport_present=r["viewport_present"],
                schema_types=r["schema_types"] or [],
                images_total=r["images_total"] or 0,
                images_missing_alt=r["images_missing_alt"] or 0,
                internal_outlinks=r["internal_outlinks"] or 0,
                text_hash=bytes(r["text_hash"]) if r["text_hash"] else None,
                redirect_chain=r["redirect_chain"], depth=r["depth"],
                render_mode=r["render_mode"] or "http",
            )
            for r in rows
        ]

    async def _linked(self, crawl_id: UUID) -> set[bytes]:
        rows = await fetch_all(
            self._conn,
            "select distinct to_url_hash from page_links "
            " where crawl_id = %s and is_internal",
            (crawl_id,),
        )
        return {bytes(r["to_url_hash"]) for r in rows}

    async def _broken_links(self, crawl_id: UUID) -> dict[bytes, str]:
        """Internal links whose target the crawl fetched and found missing."""
        rows = await fetch_all(
            self._conn,
            """
            select distinct l.to_url_hash, l.to_url
              from page_links l
              join pages p on p.url_hash = l.to_url_hash
              join page_snapshots s
                on s.page_id = p.id and s.crawl_id = l.crawl_id
             where l.crawl_id = %s and l.is_internal and s.status_code = 404
            """,
            (crawl_id,),
        )
        return {bytes(r["to_url_hash"]): r["to_url"] for r in rows}

    async def _queries(
        self, website_id: UUID, start: date, end: date
    ) -> list[QueryRow]:
        rows = await fetch_all(
            self._conn,
            """
            select min(query) as phrase, sum(clicks) as clicks,
                   sum(impressions) as impressions,
                   sum(position * impressions) / nullif(sum(impressions), 0) as position
              from gsc_query_daily
             where website_id = %s and date between %s and %s
             group by query_hash
            """,
            (website_id, start, end),
        )
        return [
            QueryRow(
                phrase=r["phrase"], clicks=int(r["clicks"]),
                impressions=int(r["impressions"]),
                position=float(r["position"] or 0),
            )
            for r in rows
        ]

    async def _page_performance(
        self, website_id: UUID, start: date, end: date
    ) -> dict[bytes, PagePerformance]:
        rows = await fetch_all(
            self._conn,
            """
            select url_hash, min(url) as url, sum(clicks) as clicks,
                   sum(impressions) as impressions,
                   sum(position * impressions) / nullif(sum(impressions), 0) as position
              from gsc_page_daily
             where website_id = %s and date between %s and %s
             group by url_hash
            """,
            (website_id, start, end),
        )
        return {
            bytes(r["url_hash"]): PagePerformance(
                url_hash=bytes(r["url_hash"]), url=r["url"],
                clicks=int(r["clicks"]), impressions=int(r["impressions"]),
                position=float(r["position"] or 0),
            )
            for r in rows
        }

    # -- evaluation --------------------------------------------------------

    def _evaluate(self, ctx: AnalysisContext) -> list[Finding]:
        findings: list[Finding] = []
        for key, fn in all_rules():
            try:
                findings.extend(fn(ctx))
            except Exception:
                # One broken rule must not lose the other twenty-six findings.
                logger.exception("rule %s failed", key)
        return findings

    # -- reconciliation ----------------------------------------------------

    async def _reconcile(
        self,
        organization_id: UUID,
        website_id: UUID,
        crawl_id: UUID | None,
        ctx: AnalysisContext,
        findings: list[Finding],
    ) -> AnalysisResult:
        result = AnalysisResult(
            pages_evaluated=len(ctx.pages),
            rules_run=len(all_rules()),
            findings=findings,
        )

        existing = {
            r["fingerprint"]: r
            for r in await fetch_all(
                self._conn,
                "select id, fingerprint, status from issues where website_id = %s",
                (website_id,),
            )
        }
        seen: set[str] = set()

        for finding in findings:
            fp = finding.fingerprint
            seen.add(fp)
            previous = existing.get(fp)

            # A resolved issue that comes back is REGRESSED, not merely open:
            # the customer fixed it once and deserves to be told it returned.
            status = "open"
            if previous and previous["status"] in ("verified", "resolved"):
                status = "regressed"
                result.issues_regressed += 1
            elif previous is None:
                result.issues_new += 1

            row = await fetch_one(
                self._conn,
                """
                insert into issues (organization_id, website_id, type_key,
                                    scope_type, page_id, fingerprint, severity,
                                    status, impact_score, evidence, source,
                                    derived_from, observed_from, observed_to,
                                    calculation_version, deterministic)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'derived',%s,%s,%s,%s,true)
                on conflict (website_id, fingerprint) do update
                   set last_detected_at = now(),
                       severity = excluded.severity,
                       impact_score = excluded.impact_score,
                       evidence = excluded.evidence,
                       observed_from = excluded.observed_from,
                       observed_to = excluded.observed_to,
                       computed_at = now(),
                       -- A dismissed issue stays dismissed: the customer has
                       -- already told us their answer.
                       status = case
                           when issues.status = 'dismissed' then 'dismissed'
                           when issues.status = 'snoozed'
                                and issues.snoozed_until > now() then 'snoozed'
                           else %s end
                returning id, status
                """,
                (
                    organization_id, website_id, finding.type_key,
                    finding.scope_type, finding.page_id, fp,
                    finding.resolved_severity, status, finding.impact,
                    _json(finding.evidence),
                    _json({"crawl_id": str(crawl_id) if crawl_id else None}),
                    ctx.window_start, ctx.window_end,
                    BY_KEY[finding.type_key].rule_version,
                    status,
                ),
            )
            assert row is not None
            if row["status"] not in ("dismissed", "snoozed"):
                result.issues_open += 1

            await self._observe(row["id"], website_id, crawl_id, True,
                                finding.resolved_severity, finding.evidence)

        # Anything previously open that no rule found this time is resolved.
        for fp, previous in existing.items():
            if fp in seen or previous["status"] in ("dismissed", "resolved"):
                continue
            await self._conn.execute(
                "update issues set status = 'resolved', resolved_at = now(), "
                "       resolution_source = 'disappeared' where id = %s",
                (previous["id"],),
            )
            await self._observe(previous["id"], website_id, crawl_id, False, None, None)
            result.issues_resolved += 1

        return result

    async def _observe(
        self, issue_id, website_id, crawl_id, present, severity, evidence
    ) -> None:
        """Append-only. This is the row that proves a fix held."""
        await self._conn.execute(
            "insert into issue_observations (organization_id, issue_id, "
            "       website_id, crawl_id, present, severity, evidence) "
            "select i.organization_id, %s, %s, %s, %s, %s, %s "
            "  from issues i where i.id = %s",
            (issue_id, website_id, crawl_id, present, severity,
             _json(evidence) if evidence else None, issue_id),
        )

    # -- scoring -----------------------------------------------------------

    async def _score(
        self,
        organization_id: UUID,
        website_id: UUID,
        crawl_id: UUID | None,
        ctx: AnalysisContext,
        result: AnalysisResult,
    ) -> None:
        components = [
            Component(
                r["key"], r["label"], float(r["weight"]),
                r["data_source"], r["is_modelled"],
            )
            for r in await fetch_all(
                self._conn,
                "select key, label, weight, data_source, is_modelled "
                "  from score_components where scoring_version = %s and enabled "
                " order by key",
                (SCORING_VERSION,),
            )
        ]
        if not components:
            return

        grouped = await self._issues_by_component(website_id)
        measured = {
            "search_performance": await self._search_performance(ctx),
            "analytics_coverage": await self._analytics_coverage(website_id, ctx),
        }

        card = score_components(
            components, grouped, evaluated_pages=len(ctx.pages), measured=measured
        )
        result.score_total = card.total

        scores = card.by_key()
        await self._conn.execute(
            """
            insert into score_snapshots (organization_id, website_id, as_of,
                scoring_version, total, technical_health, search_performance,
                content_health, analytics_coverage, ai_visibility, components,
                crawl_id, source, derived_from, observed_from, observed_to,
                deterministic)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'derived',%s,%s,%s,true)
            on conflict (website_id, as_of, scoring_version) do update
               set total = excluded.total,
                   technical_health = excluded.technical_health,
                   search_performance = excluded.search_performance,
                   content_health = excluded.content_health,
                   analytics_coverage = excluded.analytics_coverage,
                   ai_visibility = excluded.ai_visibility,
                   components = excluded.components,
                   computed_at = now()
            """,
            (
                organization_id, website_id, self._today, SCORING_VERSION,
                card.total,
                _score_of(scores, "technical_health"),
                _score_of(scores, "search_performance"),
                _score_of(scores, "content_health"),
                _score_of(scores, "analytics_coverage"),
                _score_of(scores, "ai_visibility"),
                _json(card.as_components_json()),
                crawl_id,
                _json({"crawl_id": str(crawl_id) if crawl_id else None,
                       "issues": result.issues_open}),
                ctx.window_start, ctx.window_end,
            ),
        )

    async def _issues_by_component(
        self, website_id: UUID
    ) -> dict[str, list[dict[str, Any]]]:
        rows = await fetch_all(
            self._conn,
            """
            select t.category, i.type_key, i.severity, t.score_weight as weight,
                   t.scope_type, count(*) as affected
              from issues i join issue_types t on t.key = i.type_key
             where i.website_id = %s
               and i.status in ('open', 'regressed', 'applied')
             group by t.category, i.type_key, i.severity, t.score_weight, t.scope_type
            """,
            (website_id,),
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            component = CATEGORY_TO_COMPONENT.get(r["category"])
            if component:
                grouped.setdefault(component, []).append(dict(r))
        return grouped

    async def _search_performance(self, ctx: AnalysisContext) -> float | None:
        """Measured, not penalty-based: how the site is actually doing.

        None when there is no Search Console data, so the component is absent
        rather than scored zero for a site that simply has not connected yet.
        """
        if not ctx.page_performance:
            return None

        now = sum(p.clicks for p in ctx.page_performance.values())
        before = sum(p.clicks for p in ctx.prior_page_performance.values())

        if not ctx.prior_page_performance:
            # No history to compare against. A neutral starting point beats
            # inventing a trend from one window.
            return 70.0

        if before == 0:
            return 85.0 if now > 0 else 50.0

        change = (now - before) / before
        # Maps a -50%..+50% swing onto 20..100, centred at 60 for flat.
        return max(0.0, min(100.0, 60 + change * 80))

    async def _analytics_coverage(
        self, website_id: UUID, ctx: AnalysisContext
    ) -> float | None:
        row = await fetch_one(
            self._conn,
            "select exists(select 1 from ga4_daily where website_id = %s)\n"
            "         as has_data,\n"
            "       exists(select 1 from ga4_goal_events where website_id = %s)\n"
            "         as has_goals",
            (website_id, website_id),
        )
        if not row or not row["has_data"]:
            return None
        # Connected is most of the value; a mapped goal is the rest, because
        # without one there are no outcomes to report.
        return 100.0 if row["has_goals"] else 65.0


def _json(value) -> str:
    import json

    return json.dumps(value, default=str)


def _score_of(scores, key):
    component = scores.get(key)
    return component.score if component else None
