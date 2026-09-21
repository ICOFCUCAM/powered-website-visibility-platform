"""The weekly plan.

Two halves with different authors, and keeping them apart is the whole design:

    SELECTION AND RANKING are code. Open findings are grouped by problem kind,
    ranked by summed `impact_score` — estimated recoverable clicks, the one
    currency every rule reports in — and the top four become the week's
    priorities. Run it twice on unchanged data and you get the same four in
    the same order, because there is no model in that path.

    PROSE is generated, and only after the row it belongs to already exists.
    A generation that renames a priority is fine. A generation that reorders,
    drops or invents one is rejected and the plan falls back to its templated
    text — rejected by comparing refs, not by trusting the instruction.

The window is computed exactly as the dashboard computes it, from the same
repository, so "the email disagrees with the screen" cannot happen quietly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one
from api.ai.budget import budget_for
from api.ai.cache import render_prompt
from api.ai.metering import DEFAULT_SESSION, MeterSession, record_call
from api.ai.prompts import issue_explanation, weekly_plan
from api.ai.providers import LLMProvider, ProviderError
from api.ai.validate import validate
from api.analysis.catalogue import BY_KEY
from api.analysis.runner import ANALYSIS_WINDOW_DAYS, GSC_LAG_DAYS
from api.analysis.scoring import SCORING_VERSION
from api.repositories.postgres.performance import PerformanceRepository, Totals

logger = logging.getLogger("visibility_hub.ai")

PURPOSE = "weekly_plan"


def window_for(as_of: date) -> tuple[date, date, date, date]:
    """(start, end, prior_start, prior_end), identical to the dashboard's."""
    end = as_of - timedelta(days=GSC_LAG_DAYS)
    start = end - timedelta(days=ANALYSIS_WINDOW_DAYS - 1)
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=ANALYSIS_WINDOW_DAYS - 1)
    return start, end, prior_start, prior_end


def week_start_for(as_of: date) -> date:
    """The Monday of the week the report covers."""
    return as_of - timedelta(days=as_of.weekday())


@dataclass(frozen=True, slots=True)
class Priority:
    """One ranked group of findings. Everything here is computed, not written."""

    ref: str
    rank: int
    type_key: str
    count: int
    impact: Decimal
    estimated_clicks_delta: int
    effort: str
    confidence: float
    kind: str
    issue_ids: list[UUID]
    #: The pages or searches this priority is about. A page-scoped finding
    #: contributes a URL, a keyword-scoped one contributes the search phrase,
    #: and they live in one list because a customer reading "and 1 more" under
    #: an empty list is being told nothing at all.
    examples: list[str]
    evidence: dict[str, Any]

    @property
    def title(self) -> str:
        return weekly_plan.action_title(self.type_key, self.count)


@dataclass(frozen=True, slots=True)
class Prose:
    title: str
    why: str
    how: list[str]
    source: str


@dataclass
class PlanResult:
    plan_id: UUID
    week_start: date
    window: tuple[date, date]
    summary: str
    priorities: list[Priority] = field(default_factory=list)
    prose: dict[str, Prose] = field(default_factory=dict)
    model: str = "template"
    model_provider: str = "template"
    fallback_reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def generated(self) -> bool:
        return self.fallback_reason is None and self.model_provider != "template"


class WeeklyPlanService:
    def __init__(
        self,
        conn: AsyncConnection,
        *,
        organization_id: UUID,
        website_id: UUID,
        provider: LLMProvider | None = None,
        meter: MeterSession = DEFAULT_SESSION,
    ) -> None:
        self._conn = conn
        self._meter_session = meter
        self._organization_id = organization_id
        self._website_id = website_id
        self._provider = provider
        self._performance = PerformanceRepository(conn)

    # -- ranking, which is code ------------------------------------------
    async def rank(self) -> list[Priority]:
        rows = await fetch_all(
            self._conn,
            """
            select i.type_key, t.effort,
                   count(*) as n,
                   sum(i.impact_score) as impact,
                   array_agg(i.id order by i.impact_score desc, i.id)
                       as issue_ids,
                   -- The page row is the best source, but a finding derived
                   -- from Search Console can name a URL we never crawled, and
                   -- a keyword finding names no page at all. Falling through
                   -- to the evidence is what stops those rendering blank.
                   array_remove(
                       array_agg(
                           coalesce(p.url, i.evidence->>'url',
                                    i.evidence->>'query')
                           order by i.impact_score desc, i.id),
                       null) as examples,
                   (array_agg(i.evidence order by i.impact_score desc, i.id))[1]
                       as evidence
              from issues i
              join issue_types t on t.key = i.type_key
              left join pages p on p.id = i.page_id
             where i.website_id = %s and i.status in ('open','regressed')
             group by i.type_key, t.effort
             -- Every tie-break is deterministic, down to the type key, so the
             -- same evidence produces the same four priorities in the same
             -- order on every run.
             order by sum(i.impact_score) desc, count(*) desc, i.type_key
             limit %s
            """,
            (self._website_id, weekly_plan.MAX_PRIORITIES),
        )
        return [
            Priority(
                ref=f"f{index + 1}",
                rank=index + 1,
                type_key=row["type_key"],
                count=int(row["n"]),
                impact=Decimal(row["impact"] or 0),
                estimated_clicks_delta=int(round(float(row["impact"] or 0))),
                effort=row["effort"],
                confidence=weekly_plan.confidence_for(row["type_key"]),
                kind=weekly_plan.kind_for(row["type_key"]),
                issue_ids=list(row["issue_ids"] or []),
                examples=list(row["examples"] or [])[:10],
                evidence=dict(row["evidence"] or {}),
            )
            for index, row in enumerate(rows)
        ]

    # -- memory, which is a join ------------------------------------------
    async def last_week(self, week_start: date) -> dict[str, Any]:
        previous = week_start - timedelta(days=7)
        plan = await fetch_one(
            self._conn,
            "select id, week_start, summary_md from plans "
            " where website_id = %s and week_start = %s",
            (self._website_id, previous),
        )
        if plan is None:
            return {"had_plan": False, "week_start": previous.isoformat()}

        items = await fetch_all(
            self._conn,
            """
            select r.rank, r.title, r.status, r.kind, r.targets,
                   r.estimated_clicks_delta,
                   coalesce(i.type_key, r.targets->>'type_key') as type_key
              from recommendations r
              left join issues i on i.id = r.issue_id
             where r.plan_id = %s
             order by r.rank
            """,
            (plan["id"],),
        )
        current = await fetch_all(
            self._conn,
            """
            select type_key, count(*) as n from issues
             where website_id = %s and status in ('open','regressed')
             group by type_key
            """,
            (self._website_id,),
        )
        open_now = {row["type_key"]: int(row["n"]) for row in current}

        movement = await fetch_one(
            self._conn,
            """
            select count(*) filter (where status in ('resolved','verified')
                                      and resolved_at >= %s) as resolved,
                   count(*) filter (where status = 'regressed') as regressed
              from issues where website_id = %s
            """,
            (previous, self._website_id),
        )

        return {
            "had_plan": True,
            "week_start": previous.isoformat(),
            "items": [
                {
                    "title": row["title"],
                    "status": row["status"],
                    "type_key": row["type_key"],
                    "pages_then": int(
                        (row["targets"] or {}).get("count") or 0
                    ),
                    "pages_now": open_now.get(row["type_key"], 0),
                }
                for row in items
            ],
            "issues_resolved_since": int(movement["resolved"]) if movement else 0,
            "issues_regressed_now": int(movement["regressed"]) if movement else 0,
        }

    # -- the prompt payload ------------------------------------------------
    async def payload_for(
        self, priorities: list[Priority], *, as_of: date
    ) -> dict[str, Any]:
        start, end, prior_start, prior_end = window_for(as_of)
        website = await fetch_one(
            self._conn,
            "select domain, name from websites where id = %s",
            (self._website_id,),
        )
        now = Totals.of(await self._performance.totals(self._website_id, start, end))
        before = Totals.of(
            await self._performance.totals(self._website_id, prior_start, prior_end)
        )
        return {
            "website": {
                "domain": str(website["domain"]) if website else "",
                "name": (website or {}).get("name") or "",
            },
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "performance": _performance_block(now, before),
            "findings": [
                {
                    "ref": priority.ref,
                    "problem": BY_KEY[priority.type_key].title,
                    "type_key": priority.type_key,
                    "suggested_title": priority.title,
                    "pages_affected": priority.count,
                    "estimated_clicks_delta": priority.estimated_clicks_delta,
                    "effort": priority.effort,
                    "examples": priority.examples[:5],
                    "evidence": priority.evidence,
                }
                for priority in priorities
            ],
            "last_week": await self.last_week(week_start_for(as_of)),
        }

    # -- generation, which is prose ---------------------------------------
    async def generate(self, *, as_of: date | None = None) -> PlanResult:
        as_of = as_of or date.today()
        week_start = week_start_for(as_of)
        start, end, _, _ = window_for(as_of)
        priorities = await self.rank()
        payload = await self.payload_for(priorities, as_of=as_of)

        summary, prose, model, provider_name, reason = await self._prose(
            priorities, payload
        )

        plan_id = await self._store(
            week_start=week_start,
            window=(start, end),
            summary=summary,
            priorities=priorities,
            prose=prose,
            model=model,
            model_provider=provider_name,
            fallback_reason=reason,
        )
        return PlanResult(
            plan_id=plan_id,
            week_start=week_start,
            window=(start, end),
            summary=summary,
            priorities=priorities,
            prose=prose,
            model=model,
            model_provider=provider_name,
            fallback_reason=reason,
            payload=payload,
        )

    async def _prose(
        self, priorities: list[Priority], payload: dict[str, Any]
    ) -> tuple[str, dict[str, Prose], str, str, str | None]:
        def templated(reason: str) -> tuple[str, dict[str, Prose], str, str, str]:
            return (
                _template_summary(payload),
                _template_prose(priorities),
                "template",
                "template",
                reason,
            )

        if not priorities:
            return templated("no_findings")
        if self._provider is None:
            return templated("no_provider")

        # The weekly plan runs even over budget: it is the product, and
        # withholding it would punish the customer for a crawl that generated
        # a lot of explanations earlier in the month.
        budget = await budget_for(self._conn, self._organization_id)
        if not budget.allows(essential=True):  # pragma: no cover - always true
            return templated("budget_exhausted")

        try:
            generation = await self._provider.generate(
                tier=weekly_plan.TIER,
                system=weekly_plan.SYSTEM,
                prompt=render_prompt(payload),
                schema=weekly_plan.SCHEMA,
            )
        except ProviderError as exc:
            logger.warning("weekly plan generation failed: %s", exc)
            await self._meter(status="error", model="unknown", provider="anthropic")
            return templated("provider_error")

        shape_errors, invented = validate(
            generation.data, weekly_plan.SCHEMA, payload
        )
        refs = [item.get("ref") for item in generation.data.get("priorities", [])]
        reordered = refs != [priority.ref for priority in priorities]

        if shape_errors or invented or reordered:
            logger.warning(
                "weekly plan refused: shape=%s figures=%s reordered=%s",
                shape_errors, invented, reordered,
            )
            await self._meter(
                status="refused",
                model=generation.model,
                provider=generation.model_provider,
                generation=generation,
            )
            if reordered and not (shape_errors or invented):
                return templated("reordered_priorities")
            return templated(
                "unsupported_numbers" if invented else "validation_failed"
            )

        await self._meter(
            model=generation.model,
            provider=generation.model_provider,
            generation=generation,
        )
        by_ref = {
            item["ref"]: Prose(
                title=item["title"],
                why=item["why"],
                how=list(item["how"]),
                source="model",
            )
            for item in generation.data["priorities"]
        }
        return (
            generation.data["summary"],
            by_ref,
            generation.model,
            generation.model_provider,
            None,
        )

    async def _meter(
        self,
        *,
        model: str,
        provider: str,
        status: str = "ok",
        generation: Any = None,
    ) -> None:
        await record_call(
            self._meter_session,
            organization_id=self._organization_id,
            website_id=self._website_id,
            purpose=PURPOSE,
            prompt_version=weekly_plan.VERSION,
            model=model,
            model_provider=provider,
            derived_from={"website_id": str(self._website_id)},
            status=status,
            input_tokens=getattr(generation, "input_tokens", 0),
            output_tokens=getattr(generation, "output_tokens", 0),
            cached_input_tokens=getattr(generation, "cached_input_tokens", 0),
            cost_usd=getattr(generation, "cost_usd", None),
            latency_ms=getattr(generation, "latency_ms", None),
            attached_to_table="plans",
        )

    # -- storage ------------------------------------------------------------
    async def _store(
        self,
        *,
        week_start: date,
        window: tuple[date, date],
        summary: str,
        priorities: list[Priority],
        prose: dict[str, Prose],
        model: str,
        model_provider: str,
        fallback_reason: str | None,
    ) -> UUID:
        start, end = window
        row = await fetch_one(
            self._conn,
            """
            insert into plans (organization_id, website_id, week_start,
                               summary_md, scoring_version, prompt_version,
                               model, model_provider, deterministic,
                               fallback_reason, source, derived_from,
                               observed_from, observed_to)
            values (%s,%s,%s,%s,%s,%s,%s,%s,false,%s,'derived',%s,%s,%s)
            on conflict (website_id, week_start) do update
               set summary_md = excluded.summary_md,
                   prompt_version = excluded.prompt_version,
                   model = excluded.model,
                   model_provider = excluded.model_provider,
                   fallback_reason = excluded.fallback_reason,
                   derived_from = excluded.derived_from,
                   observed_from = excluded.observed_from,
                   observed_to = excluded.observed_to,
                   generated_at = now()
            returning id
            """,
            (
                self._organization_id,
                self._website_id,
                week_start,
                summary,
                SCORING_VERSION,
                weekly_plan.VERSION,
                model,
                model_provider,
                fallback_reason,
                json.dumps(
                    {
                        "issue_ids": [
                            str(issue_id)
                            for priority in priorities
                            for issue_id in priority.issue_ids
                        ][:200]
                    }
                ),
                start,
                end,
            ),
        )
        plan_id = row["id"]

        # Regenerating a week replaces its recommendations rather than adding
        # to them. The statuses a customer set are carried across by issue, so
        # "I've done this" survives a re-run.
        existing = await fetch_all(
            self._conn,
            "select issue_id, status from recommendations where plan_id = %s",
            (plan_id,),
        )
        previous_status = {
            row["issue_id"]: row["status"] for row in existing if row["issue_id"]
        }
        await self._conn.execute(
            "delete from recommendations where plan_id = %s", (plan_id,)
        )

        for priority in priorities:
            words = prose.get(priority.ref)
            issue_id = priority.issue_ids[0] if priority.issue_ids else None
            await self._conn.execute(
                """
                insert into recommendations
                    (organization_id, website_id, plan_id, issue_id, kind, rank,
                     title, body_md, how_to_md, impact_score,
                     estimated_clicks_delta, effort, confidence, targets,
                     status, prose_source, source, derived_from,
                     observed_from, observed_to, calculation_version,
                     deterministic)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        'derived',%s,%s,%s,%s,true)
                """,
                (
                    self._organization_id,
                    self._website_id,
                    plan_id,
                    issue_id,
                    priority.kind,
                    priority.rank,
                    (words.title if words else priority.title) or priority.title,
                    words.why if words else "",
                    _numbered(words.how if words else []),
                    priority.impact,
                    priority.estimated_clicks_delta,
                    priority.effort,
                    priority.confidence,
                    json.dumps(
                        {
                            "type_key": priority.type_key,
                            "count": priority.count,
                            "issue_ids": [str(i) for i in priority.issue_ids][:50],
                            "examples": priority.examples,
                        }
                    ),
                    previous_status.get(issue_id, "OPEN"),
                    words.source if words else "template",
                    json.dumps(
                        {"issue_ids": [str(i) for i in priority.issue_ids][:50]}
                    ),
                    start,
                    end,
                    SCORING_VERSION,
                ),
            )
        return plan_id


# ---------------------------------------------------------------------------
# The fallback
# ---------------------------------------------------------------------------
def _performance_block(now: Totals, before: Totals) -> dict[str, Any]:
    def delta(a: float | int | None, b: float | int | None) -> float | None:
        if a is None or b is None:
            return None
        return round(a - b, 4)

    return {
        "clicks": now.clicks,
        "impressions": now.impressions,
        "ctr": round(now.ctr, 4) if now.ctr is not None else None,
        "position": round(now.position, 1) if now.position is not None else None,
        "previous": {
            "clicks": before.clicks,
            "impressions": before.impressions,
            "ctr": round(before.ctr, 4) if before.ctr is not None else None,
            "position": round(before.position, 1)
            if before.position is not None
            else None,
        },
        "change": {
            "clicks": now.clicks - before.clicks,
            "impressions": now.impressions - before.impressions,
            "ctr": delta(now.ctr, before.ctr),
            "position": delta(now.position, before.position),
        },
        # Said explicitly rather than inferred from zeroes: a site with no
        # Search Console history and a site with no clicks look identical in
        # the numbers and are completely different situations.
        "has_history": before.impressions > 0,
    }


def _template_summary(payload: dict[str, Any]) -> str:
    performance = payload.get("performance", {})
    last = payload.get("last_week", {})
    clicks = f"{int(performance.get('clicks', 0)):,}"
    change = (performance.get("change") or {}).get("clicks")

    if not performance.get("has_history"):
        opening = (
            f"Search Console recorded {clicks} clicks in this period. "
            "There is no earlier period to compare against yet."
        )
    elif change is None or change == 0:
        opening = f"Clicks held steady at {clicks} for the period."
    elif change > 0:
        opening = f"Clicks rose by {int(change):,} to {clicks} for the period."
    else:
        opening = f"Clicks fell by {abs(int(change)):,} to {clicks} for the period."

    if not last.get("had_plan"):
        # "Nothing to compare" would contradict the click comparison in the
        # sentence before it. What is missing is an earlier PLAN, not earlier
        # figures.
        return (
            f"{opening} This is your first plan, so there is no earlier one to "
            "measure progress against."
        )

    resolved = last.get("issues_resolved_since", 0)
    if resolved:
        return f"{opening} {resolved} of the problems in last week's plan are fixed."
    return f"{opening} Nothing from last week's plan has been fixed yet."


def _template_prose(priorities: list[Priority]) -> dict[str, Prose]:
    prose: dict[str, Prose] = {}
    for priority in priorities:
        issue_type = BY_KEY[priority.type_key]
        prose[priority.ref] = Prose(
            title=priority.title,
            why=issue_type.summary,
            how=list(
                issue_explanation.TEMPLATE_FIXES.get(
                    priority.type_key, issue_explanation.GENERIC_FIX
                )
            ),
            source="template",
        )
    return prose


def _numbered(steps: list[str]) -> str:
    return "\n".join(f"{index}. {step}" for index, step in enumerate(steps, 1))
