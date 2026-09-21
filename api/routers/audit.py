"""The audit (V1 spec s20).

Every issue carries what it is, why it matters, which pages it affects and
what to do — and, because the fingerprint is stable, its history: when it was
first seen, whether it was fixed before, and whether it came back.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, Field

from api.adapters.db import fetch_all, fetch_one
from api.deps import ConnectionDep, WebsiteScopeDep
from api.domain.errors import NotFound

router = APIRouter(prefix="/websites", tags=["audit"])

OPEN_STATUSES = ("open", "regressed", "applied")


class IssueOut(BaseModel):
    id: str
    type_key: str
    title: str
    summary: str
    category: str
    severity: str
    status: str
    scope_type: str
    impact_score: float
    effort: str
    url: str | None
    evidence: dict[str, Any]
    first_detected_at: datetime
    last_detected_at: datetime


class AuditCounts(BaseModel):
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    info: int = 0


class AuditOut(BaseModel):
    checks_run: int
    issues_open: int
    counts: AuditCounts
    by_category: dict[str, int]
    issues: list[IssueOut]
    # Absent rather than zero when there is no Search Console data: several
    # rules simply cannot run, and saying so beats implying a clean bill.
    search_data_available: bool


class ObservationOut(BaseModel):
    observed_at: datetime
    present: bool
    severity: str | None


class IssueDetailOut(IssueOut):
    history: list[ObservationOut]
    rule_version: str


class ResolveIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


_ISSUE_SQL = """
    select i.id, i.type_key, i.severity, i.status, i.scope_type,
           i.impact_score, i.evidence, i.first_detected_at, i.last_detected_at,
           i.calculation_version, t.title, t.summary, t.category, t.effort,
           p.url
      from issues i
      join issue_types t on t.key = i.type_key
      left join pages p on p.id = i.page_id
     where i.website_id = %s
"""


def _shape(row) -> dict:
    return {
        "id": str(row["id"]),
        "type_key": row["type_key"],
        "title": row["title"],
        "summary": row["summary"],
        "category": row["category"],
        "severity": row["severity"],
        "status": row["status"],
        "scope_type": row["scope_type"],
        "impact_score": float(row["impact_score"] or 0),
        "effort": row["effort"],
        "url": row["url"],
        "evidence": row["evidence"] or {},
        "first_detected_at": row["first_detected_at"],
        "last_detected_at": row["last_detected_at"],
    }


@router.get("/{website_id}/audit", response_model=AuditOut)
async def audit(
    scope: WebsiteScopeDep,
    conn: ConnectionDep,
    category: Annotated[str | None, Query()] = None,
    severity: Annotated[str | None, Query()] = None,
    status_filter: Annotated[
        Literal["open", "all", "resolved"], Query(alias="status")
    ] = "open",
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> AuditOut:
    sql = _ISSUE_SQL
    params: list[Any] = [scope.website.id]

    if status_filter == "open":
        sql += " and i.status = any(%s)"
        params.append(list(OPEN_STATUSES))
    elif status_filter == "resolved":
        sql += " and i.status in ('resolved','verified')"

    if category:
        sql += " and t.category = %s"
        params.append(category)
    if severity:
        sql += " and i.severity = %s"
        params.append(severity)

    # Ranked by estimated additional clicks — the one currency every rule
    # reports in — then by severity for the many findings worth nothing
    # measurable on their own.
    sql += """
        order by i.impact_score desc,
                 case i.severity when 'critical' then 0 when 'high' then 1
                                 when 'medium' then 2 when 'low' then 3
                                 else 4 end
        limit %s
    """
    params.append(limit)

    rows = await fetch_all(conn, sql, tuple(params))

    tallies = await fetch_all(
        conn,
        """
        select i.severity, t.category, count(*) as n
          from issues i join issue_types t on t.key = i.type_key
         where i.website_id = %s and i.status = any(%s)
         group by i.severity, t.category
        """,
        (scope.website.id, list(OPEN_STATUSES)),
    )

    counts = AuditCounts()
    by_category: dict[str, int] = {}
    for row in tallies:
        setattr(counts, row["severity"], getattr(counts, row["severity"]) + row["n"])
        by_category[row["category"]] = by_category.get(row["category"], 0) + row["n"]

    checks = await fetch_one(
        conn, "select count(*) as n from issue_types where active", ()
    )
    has_search = await fetch_one(
        conn,
        "select exists(select 1 from gsc_page_daily where website_id = %s) as ok",
        (scope.website.id,),
    )

    return AuditOut(
        checks_run=int(checks["n"]) if checks else 0,
        issues_open=sum(by_category.values()),
        counts=counts,
        by_category=by_category,
        issues=[IssueOut(**_shape(r)) for r in rows],
        search_data_available=bool(has_search and has_search["ok"]),
    )


@router.get("/{website_id}/audit/{issue_id}", response_model=IssueDetailOut)
async def issue_detail(
    scope: WebsiteScopeDep,
    issue_id: Annotated[UUID, Path()],
    conn: ConnectionDep,
) -> IssueDetailOut:
    row = await fetch_one(
        conn, _ISSUE_SQL + " and i.id = %s", (scope.website.id, issue_id)
    )
    if row is None:
        raise NotFound("We couldn't find that issue.")

    history = await fetch_all(
        conn,
        "select observed_at, present, severity from issue_observations "
        " where issue_id = %s order by observed_at desc limit 50",
        (issue_id,),
    )
    return IssueDetailOut(
        **_shape(row),
        rule_version=row["calculation_version"],
        history=[ObservationOut(**h) for h in history],
    )


@router.post("/{website_id}/audit/{issue_id}/resolve", response_model=IssueDetailOut)
async def mark_resolved(
    scope: WebsiteScopeDep,
    issue_id: Annotated[UUID, Path()],
    body: ResolveIn,
    conn: ConnectionDep,
) -> IssueDetailOut:
    """The hinge of the loop.

    Marks the issue applied and records WHO said so. It is not resolved until
    a later crawl fails to find it — the customer's word starts the
    verification, it does not end it.
    """
    scope.require_write()
    updated = await fetch_one(
        conn,
        """
        update issues
           set status = 'applied', resolved_by = %s,
               resolution_source = 'user_marked'
         where id = %s and website_id = %s
           and status in ('open','regressed','snoozed')
        returning id
        """,
        (scope.membership.user_id, issue_id, scope.website.id),
    )
    if updated is None:
        raise NotFound("We couldn't find that issue.")

    await conn.execute(
        """
        insert into actions (organization_id, website_id, issue_id, capability,
                             provider_key, target_kind, target_ref, status,
                             before_state, applied_by, applied_at, notes)
        values (%s, %s, %s, 'manual.mark_fixed', 'manual', 'page', %s,
                'applied', %s, %s, now(), %s)
        """,
        (
            scope.website.organization_id, scope.website.id, issue_id,
            str(issue_id), _json({"status": "open"}),
            scope.membership.user_id, body.note,
        ),
    )
    return await issue_detail(scope, issue_id, conn)


def _json(value) -> str:
    import json

    return json.dumps(value)
