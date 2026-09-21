"""Generating and sending a weekly report.

The `generate_weekly_report` job from the V1 spec (s31). It runs after the
week's crawl, sync and analysis, so the plan it builds is over settled data
rather than a scan that is still in progress.

Idempotent per (website, week): running it twice replaces the draft rather
than producing a second one. That matters because the nightly schedule retries
and because a customer pressing "regenerate" should get one report, not a pile.

A report that has been SENT is never silently rewritten. What went out is what
is on record, and a re-run after a send writes a new draft only if the caller
asks for it explicitly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_one
from api.ai.metering import DEFAULT_SESSION, MeterSession
from api.ai.plan import WeeklyPlanService
from api.ai.providers import LLMProvider
from api.crawler.storage import ArtifactStore
from api.reports import render
from api.reports.mail import Mailer, Message
from api.reports.weekly import ReportFigures, assemble
from api.repositories.postgres.organizations import member_emails

logger = logging.getLogger("visibility_hub.reports")

KIND = "weekly"


def report_key(website_id: UUID, week_start: date) -> str:
    return f"reports/{website_id}/{week_start.isoformat()}.html.gz"


@dataclass
class ReportResult:
    report_id: UUID
    plan_id: UUID
    figures: ReportFigures
    subject: str
    html: str
    text: str
    status: str
    html_key: str | None


async def generate(
    conn: AsyncConnection,
    *,
    organization_id: UUID,
    website_id: UUID,
    as_of: date | None = None,
    provider: LLMProvider | None = None,
    meter: MeterSession = DEFAULT_SESSION,
    store: ArtifactStore | None = None,
    dashboard_url: str | None = None,
    replace_sent: bool = False,
) -> ReportResult:
    as_of = as_of or date.today()

    plan = await WeeklyPlanService(
        conn,
        organization_id=organization_id,
        website_id=website_id,
        provider=provider,
        meter=meter,
    ).generate(as_of=as_of)

    figures = await assemble(conn, website_id=website_id, plan=plan, as_of=as_of)
    body_html = render.html(figures, dashboard_url=dashboard_url)
    body_text = render.text(figures, dashboard_url=dashboard_url)
    subject = render.subject(figures)

    existing = await fetch_one(
        conn,
        "select id, status, html_key from reports "
        " where website_id = %s and kind = %s and period_start = %s",
        (website_id, KIND, plan.week_start),
    )
    if existing and existing["status"] == "sent" and not replace_sent:
        logger.info(
            "weekly report already sent, leaving it on record: website=%s week=%s",
            website_id, plan.week_start,
        )
        return ReportResult(
            report_id=existing["id"],
            plan_id=plan.plan_id,
            figures=figures,
            subject=subject,
            html=body_html,
            text=body_text,
            status="sent",
            html_key=existing["html_key"],
        )

    html_key: str | None = None
    if store is not None:
        html_key = await store.put(
            report_key(website_id, plan.week_start), body_html.encode()
        )

    row = await fetch_one(
        conn,
        """
        insert into reports (organization_id, website_id, kind, period_start,
                             period_end, plan_id, html_key, status, subject,
                             payload)
        values (%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s)
        on conflict (website_id, kind, period_start) do update
           set period_end = excluded.period_end,
               plan_id = excluded.plan_id,
               html_key = excluded.html_key,
               subject = excluded.subject,
               payload = excluded.payload,
               status = 'draft',
               error = null,
               generated_at = now()
        returning id
        """,
        (
            organization_id,
            website_id,
            KIND,
            plan.week_start,
            figures.period_end,
            plan.plan_id,
            html_key,
            subject,
            json.dumps(figures.as_payload(), default=str),
        ),
    )
    return ReportResult(
        report_id=row["id"],
        plan_id=plan.plan_id,
        figures=figures,
        subject=subject,
        html=body_html,
        text=body_text,
        status="draft",
        html_key=html_key,
    )


async def recipients_for(conn: AsyncConnection, organization_id: UUID) -> list[str]:
    """Who the weekly report goes to. See `member_emails`."""
    return await member_emails(conn, organization_id)


async def send(
    conn: AsyncConnection,
    *,
    report_id: UUID,
    mailer: Mailer,
    recipients: list[str],
    store: ArtifactStore | None = None,
) -> None:
    """Deliver, then record. Never the other way round.

    A failure is written to the row with its reason and the status set to
    'failed', so a report that did not arrive looks different from one that
    did — which is the whole point of storing a status at all.
    """
    report = await fetch_one(
        conn,
        "select id, subject, html_key, payload, status from reports where id = %s",
        (report_id,),
    )
    if report is None:
        raise LookupError("report not found")
    if not recipients:
        raise ValueError("no recipients")

    body_html = await _html_for(report, store)
    message = Message(
        to=recipients,
        subject=report["subject"] or "Your weekly website report",
        text=_text_fallback(report),
        html=body_html,
    )

    try:
        await mailer.send(message)
    except Exception as exc:
        await conn.execute(
            "update reports set status = 'failed', error = %s where id = %s",
            (f"{type(exc).__name__}: {exc}"[:500], report_id),
        )
        raise

    await conn.execute(
        "update reports set status = 'sent', sent_at = now(), error = null, "
        "       recipients = %s where id = %s",
        (recipients, report_id),
    )


async def _html_for(report: dict[str, Any], store: ArtifactStore | None) -> str:
    if report["html_key"] and store is not None:
        body = await store.get(report["html_key"])
        if body is not None:
            return body.decode()
    # The stored payload is the system of record for the figures, so a report
    # whose artifact is missing can still be rebuilt rather than lost.
    return _rebuild(report["payload"] or {})


def _rebuild(payload: dict[str, Any]) -> str:
    figures = figures_from_payload(payload)
    return render.html(figures)


def _text_fallback(report: dict[str, Any]) -> str:
    return render.text(figures_from_payload(report["payload"] or {}))


def figures_from_payload(payload: dict[str, Any]) -> ReportFigures:
    from api.reports.weekly import Movement, PriorityView

    period = payload.get("period") or {}
    return ReportFigures(
        website=payload.get("website") or {},
        period_start=date.fromisoformat(period.get("start") or date.today().isoformat()),
        period_end=date.fromisoformat(period.get("end") or date.today().isoformat()),
        week_start=date.fromisoformat(
            payload.get("week_start") or date.today().isoformat()
        ),
        score=payload.get("score"),
        search=payload.get("search"),
        summary=payload.get("summary") or "",
        priorities=[
            PriorityView(
                rank=item["rank"],
                title=item["title"],
                why=item.get("why") or "",
                how=list(item.get("how") or []),
                count=int(item.get("count") or 0),
                estimated_clicks_delta=int(item.get("estimated_clicks_delta") or 0),
                effort=item.get("effort") or "medium",
                examples=list(item.get("examples") or []),
                prose_source=item.get("prose_source") or "template",
            )
            for item in payload.get("priorities") or []
        ],
        movements=[
            Movement(kind=m["kind"], title=m["title"], url=m.get("url"))
            for m in payload.get("movements") or []
        ],
        fallback_reason=payload.get("fallback_reason"),
    )
