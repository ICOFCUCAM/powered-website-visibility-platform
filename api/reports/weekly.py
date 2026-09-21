"""Assembling the weekly report.

Every figure here comes from the repositories the dashboard reads, over the
window the dashboard uses, through the helpers the dashboard calls. That is
not tidiness — it is the only arrangement in which the email and the screen
cannot disagree, and a customer who finds them disagreeing stops believing
both.

What the report adds beyond the dashboard is TIME: what was fixed this week,
what came back, and what last week's plan said. A dashboard shows a state; a
weekly report has to show a difference, or there is no reason to open it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_one
from api.ai.plan import PlanResult, window_for
from api.repositories.postgres.dashboard import DashboardRepository, pct_change
from api.repositories.postgres.performance import PerformanceRepository, Totals


@dataclass(frozen=True, slots=True)
class Movement:
    kind: str  # 'fixed' | 'returned' | 'verified'
    title: str
    url: str | None


@dataclass(frozen=True, slots=True)
class PriorityView:
    rank: int
    title: str
    why: str
    how: list[str]
    count: int
    estimated_clicks_delta: int
    effort: str
    examples: list[str]
    prose_source: str


@dataclass
class ReportFigures:
    website: dict[str, Any]
    period_start: date
    period_end: date
    week_start: date
    score: dict[str, Any] | None
    search: dict[str, Any] | None
    summary: str
    priorities: list[PriorityView] = field(default_factory=list)
    movements: list[Movement] = field(default_factory=list)
    #: Set when the prose is templated. Shown to nobody, recorded on the row;
    #: the customer is never told which model wrote their plan, only that the
    #: figures are theirs.
    fallback_reason: str | None = None

    def as_payload(self) -> dict[str, Any]:
        """Stored beside the sent HTML, so a disputed figure in a month-old
        email can be checked without re-deriving it from tables that have
        moved on."""
        return {
            "website": self.website,
            "period": {
                "start": self.period_start.isoformat(),
                "end": self.period_end.isoformat(),
            },
            "week_start": self.week_start.isoformat(),
            "score": self.score,
            "search": self.search,
            "summary": self.summary,
            "priorities": [
                {
                    "rank": p.rank,
                    "title": p.title,
                    "why": p.why,
                    "how": p.how,
                    "count": p.count,
                    "estimated_clicks_delta": p.estimated_clicks_delta,
                    "effort": p.effort,
                    "examples": p.examples,
                    "prose_source": p.prose_source,
                }
                for p in self.priorities
            ],
            "movements": [
                {"kind": m.kind, "title": m.title, "url": m.url}
                for m in self.movements
            ],
            "fallback_reason": self.fallback_reason,
        }


def _movement_kind(row: dict[str, Any]) -> str:
    if not row["present"]:
        return "fixed"
    return "returned" if row["status"] == "regressed" else "verified"


async def assemble(
    conn: AsyncConnection,
    *,
    website_id: UUID,
    plan: PlanResult,
    as_of: date,
) -> ReportFigures:
    start, end, prior_start, prior_end = window_for(as_of)
    repo = DashboardRepository(conn)
    performance = PerformanceRepository(conn)

    website_row = await fetch_one(
        conn, "select domain, name from websites where id = %s", (website_id,)
    ) or {}

    latest = await repo.latest_score(website_id)
    score = None
    if latest is not None:
        previous = await repo.score_before(
            website_id, latest["as_of"] - timedelta(days=28)
        )
        score = {
            "total": float(latest["total"]),
            "as_of": latest["as_of"].isoformat(),
            "change_pct": pct_change(
                latest["total"], previous["total"] if previous else None
            ),
            "compared_to": previous["as_of"].isoformat() if previous else None,
        }

    totals = Totals.of(await performance.totals(website_id, start, end))
    search = None
    if totals.impressions:
        before = Totals.of(
            await performance.totals(website_id, prior_start, prior_end)
        )
        search = {
            "clicks": totals.clicks,
            "impressions": totals.impressions,
            "ctr": totals.ctr,
            "position": totals.position,
            "change": {
                "clicks": pct_change(totals.clicks, before.clicks),
                "impressions": pct_change(totals.impressions, before.impressions),
                # Inverted deliberately: a position improving is the number
                # getting smaller, and every reader downstream would otherwise
                # have to remember that.
                "position": pct_change(before.position, totals.position),
            }
            if before.impressions
            else None,
        }

    movements = [
        Movement(kind=_movement_kind(row), title=row["title"], url=row["url"])
        for row in await repo.changes_since(
            website_id, plan.week_start - timedelta(days=7)
        )
    ]

    priorities = [
        PriorityView(
            rank=priority.rank,
            title=(words.title if words else priority.title),
            why=(words.why if words else ""),
            how=list(words.how) if words else [],
            count=priority.count,
            estimated_clicks_delta=priority.estimated_clicks_delta,
            effort=priority.effort,
            examples=priority.examples[:5],
            prose_source=(words.source if words else "template"),
        )
        for priority in plan.priorities
        for words in [plan.prose.get(priority.ref)]
    ]

    return ReportFigures(
        website={
            "id": str(website_id),
            "domain": str(website_row.get("domain") or ""),
            "name": website_row.get("name") or "",
        },
        period_start=start,
        period_end=end,
        week_start=plan.week_start,
        score=score,
        search=search,
        summary=plan.summary,
        priorities=priorities,
        movements=movements,
        fallback_reason=plan.fallback_reason,
    )
