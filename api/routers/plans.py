"""The plan and its recommendations (V1 spec s25).

The statuses are what make the loop close. A recommendation a customer marks
done feeds next week's `last_week` join, which is how the plan can open with
"you did this and here is what happened" rather than repeating itself. Without
these two endpoints that join reads an empty history forever and the weekly
report is a report generator.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel

from api.adapters.db import fetch_all, fetch_one
from api.deps import ConnectionDep, WebsiteScopeDep
from api.domain.errors import AppError, NotFound

router = APIRouter(prefix="/websites", tags=["plan"])

STATUSES = ("OPEN", "IN_PROGRESS", "RESOLVED", "DISMISSED")


class InvalidStatus(AppError):
    code = "invalid_status"
    status = 422
    message = "That isn't a recommendation status."
    retriable = False


class RecommendationOut(BaseModel):
    id: UUID
    rank: int
    kind: str
    title: str
    body_md: str | None
    how_to_md: str | None
    estimated_clicks_delta: int | None
    effort: str
    confidence: float
    status: str
    pages_affected: int
    #: The pages or searches this is about. A keyword finding names a
    #: search phrase rather than a URL, and both belong in one list.
    examples: list[str]
    #: 'template' or 'model'. Surfaced so the UI can label generated prose as
    #: generated; the ranking beside it is always code.
    prose_source: str


class PlanOut(BaseModel):
    id: UUID | None
    week_start: date | None
    summary_md: str | None
    generated_at: Any | None
    #: Null when a model wrote the plan and its output passed validation.
    fallback_reason: str | None
    recommendations: list[RecommendationOut]


def _recommendation(row: dict[str, Any]) -> RecommendationOut:
    targets = row["targets"] or {}
    return RecommendationOut(
        id=row["id"],
        rank=row["rank"],
        kind=row["kind"],
        title=row["title"],
        body_md=row["body_md"],
        how_to_md=row["how_to_md"],
        estimated_clicks_delta=(
            int(row["estimated_clicks_delta"])
            if row["estimated_clicks_delta"] is not None
            else None
        ),
        effort=row["effort"],
        confidence=float(row["confidence"]),
        status=row["status"],
        pages_affected=int(targets.get("count") or 0),
        examples=list(targets.get("examples") or []),
        prose_source=row["prose_source"],
    )


@router.get("/{website_id}/plan", response_model=PlanOut)
async def current_plan(scope: WebsiteScopeDep, conn: ConnectionDep) -> PlanOut:
    plan = await fetch_one(
        conn,
        "select id, week_start, summary_md, generated_at, fallback_reason "
        "  from plans where website_id = %s order by week_start desc limit 1",
        (scope.website.id,),
    )
    if plan is None:
        # Not a 404: a website with no plan yet is an ordinary state, and the
        # screen should say "we haven't written one yet" rather than error.
        return PlanOut(
            id=None,
            week_start=None,
            summary_md=None,
            generated_at=None,
            fallback_reason=None,
            recommendations=[],
        )

    rows = await fetch_all(
        conn,
        """
        select id, rank, kind, title, body_md, how_to_md,
               estimated_clicks_delta, effort, confidence, status, targets,
               prose_source
          from recommendations
         where website_id = %s and plan_id = %s
         order by rank
        """,
        (scope.website.id, plan["id"]),
    )
    return PlanOut(
        id=plan["id"],
        week_start=plan["week_start"],
        summary_md=plan["summary_md"],
        generated_at=plan["generated_at"],
        fallback_reason=plan["fallback_reason"],
        recommendations=[_recommendation(row) for row in rows],
    )


@router.get("/{website_id}/recommendations")
async def list_recommendations(
    scope: WebsiteScopeDep,
    conn: ConnectionDep,
    status: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    # Rejected rather than ignored: a filter we do not recognise returning an
    # empty list looks exactly like "you have none", which is a different and
    # wrong answer.
    if status is not None and status not in STATUSES:
        raise InvalidStatus(f"Status must be one of {', '.join(STATUSES)}.")
    rows = await fetch_all(
        conn,
        """
        select r.id, r.rank, r.kind, r.title, r.body_md, r.how_to_md,
               r.estimated_clicks_delta, r.effort, r.confidence, r.status,
               r.targets, r.prose_source, p.week_start
          from recommendations r
          join plans p on p.id = r.plan_id
         where r.website_id = %s
           and (%s::text is null or r.status = %s)
         order by p.week_start desc, r.rank
         limit 200
        """,
        (scope.website.id, status, status),
    )
    return {
        "recommendations": [
            {**_recommendation(row).model_dump(), "week_start": row["week_start"]}
            for row in rows
        ]
    }


async def _set_status(
    scope: WebsiteScopeDep,
    conn: ConnectionDep,
    recommendation_id: UUID,
    status: str,
) -> dict[str, Any]:
    scope.require_write()
    row = await fetch_one(
        conn,
        """
        update recommendations
           set status = %s,
               resolved_at = case when %s in ('RESOLVED','DISMISSED')
                                  then now() else null end
         where id = %s and website_id = %s
        returning id, status, resolved_at
        """,
        (status, status, recommendation_id, scope.website.id),
    )
    if row is None:
        raise NotFound("We couldn't find that recommendation.")
    return {"id": str(row["id"]), "status": row["status"]}


@router.post("/{website_id}/recommendations/{recommendation_id}/complete")
async def complete(
    scope: WebsiteScopeDep,
    conn: ConnectionDep,
    recommendation_id: Annotated[UUID, Path()],
) -> dict[str, Any]:
    """Marks intent, never evidence.

    Saying you fixed something does not make the issue resolved — only the
    next crawl observing it gone does that. The two are kept apart on purpose:
    the plan can then say "you marked this done and it is still there", which
    is one of the more useful sentences the product has.
    """
    return await _set_status(scope, conn, recommendation_id, "RESOLVED")


@router.post("/{website_id}/recommendations/{recommendation_id}/dismiss")
async def dismiss(
    scope: WebsiteScopeDep,
    conn: ConnectionDep,
    recommendation_id: Annotated[UUID, Path()],
) -> dict[str, Any]:
    return await _set_status(scope, conn, recommendation_id, "DISMISSED")
