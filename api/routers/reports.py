"""Weekly reports (V1 spec s27, docs/02-api.md).

Three surfaces, and the third is the awkward one:

  - generating and listing, under the normal session
  - sending, which must never record a delivery that did not happen
  - the HTML itself, which is read from a mail client where there is no
    session at all — so it is served against a signed, expiring link rather
    than behind a login the reader does not have

The HTML route re-renders from the report's stored payload rather than
fetching an artifact. The payload is the system of record for the figures
that went out, so a rendered report survives object storage losing a file, and
the request path needs no storage credentials.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from api.adapters import db
from api.adapters.db import fetch_all, fetch_one
from api.ai.deps import get_provider
from api.deps import ConnectionDep, SettingsDep, WebsiteScopeDep
from api.domain.errors import EmailNotConfigured, NotFound
from api.reports import links, render, runner
from api.reports.mail import SmtpMailer, SmtpSettings

router = APIRouter(tags=["reports"])


class ReportOut(BaseModel):
    id: UUID
    period_start: date
    period_end: date
    subject: str | None
    status: str
    generated_at: Any
    sent_at: Any | None
    recipients: list[str]
    html_url: str


def _report_out(row: dict[str, Any], settings: Any) -> ReportOut:
    expires_at, signature = links.issue(row["id"], settings.jwt_secret)
    return ReportOut(
        id=row["id"],
        period_start=row["period_start"],
        period_end=row["period_end"],
        subject=row["subject"],
        status=row["status"],
        generated_at=row["generated_at"],
        sent_at=row["sent_at"],
        recipients=list(row["recipients"] or []),
        html_url=(
            f"/api/v1/reports/{row['id']}/html"
            f"?expires={expires_at}&signature={signature}"
        ),
    )


@router.get("/websites/{website_id}/reports")
async def list_reports(
    scope: WebsiteScopeDep, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    rows = await fetch_all(
        conn,
        """
        select id, period_start, period_end, subject, status, generated_at,
               sent_at, recipients
          from reports where website_id = %s
         order by period_start desc limit 52
        """,
        (scope.website.id,),
    )
    return {"reports": [_report_out(row, settings).model_dump() for row in rows]}


@router.post("/websites/{website_id}/reports", status_code=201)
async def generate_report(
    scope: WebsiteScopeDep, conn: ConnectionDep, settings: SettingsDep
) -> dict[str, Any]:
    scope.require_write()
    result = await runner.generate(
        conn,
        organization_id=scope.website.organization_id,
        website_id=scope.website.id,
        provider=get_provider(),
        dashboard_url=f"{settings.web_base_url}/dashboard",
    )
    expires_at, signature = links.issue(result.report_id, settings.jwt_secret)
    return {
        "id": str(result.report_id),
        "plan_id": str(result.plan_id),
        "week_start": result.figures.week_start.isoformat(),
        "subject": result.subject,
        "status": result.status,
        # Said plainly rather than hidden: a plan whose prose is templated is
        # a normal outcome, and the screen offering to regenerate it later is
        # better than a silent difference in tone.
        "fallback_reason": result.figures.fallback_reason,
        "html_url": (
            f"/api/v1/reports/{result.report_id}/html"
            f"?expires={expires_at}&signature={signature}"
        ),
    }


@router.post("/websites/{website_id}/reports/{report_id}/send")
async def send_report(
    scope: WebsiteScopeDep,
    conn: ConnectionDep,
    report_id: Annotated[UUID, Path()],
) -> dict[str, Any]:
    scope.require_write()
    report = await fetch_one(
        conn,
        "select id from reports where id = %s and website_id = %s",
        (report_id, scope.website.id),
    )
    if report is None:
        raise NotFound("We couldn't find that report.")

    smtp = SmtpSettings.from_env()
    if smtp is None:
        raise EmailNotConfigured()

    recipients = await runner.recipients_for(conn, scope.website.organization_id)
    if not recipients:
        raise NotFound("There's nobody in this account to send the report to.")

    await runner.send(
        conn,
        report_id=report_id,
        mailer=SmtpMailer(smtp),
        recipients=recipients,
    )
    return {"id": str(report_id), "status": "sent", "recipients": len(recipients)}


@router.get("/reports/{report_id}/html", response_class=HTMLResponse)
async def report_html(
    report_id: Annotated[UUID, Path()],
    settings: SettingsDep,
    expires: Annotated[int, Query()],
    signature: Annotated[str, Query()],
) -> HTMLResponse:
    """Deliberately session-free, and deliberately narrow.

    A valid signature grants one rendered report until it expires. It is not a
    login: nothing else on the API accepts it, and it cannot be widened by
    changing the id in the URL because the id is what was signed.
    """
    if not links.verify(
        report_id, settings.jwt_secret, expires_at=expires, signature=signature
    ):
        # The same error for a bad signature, an expired one and a report that
        # does not exist. A distinct 404 would confirm which report ids are
        # real to anyone guessing.
        raise NotFound("This link has expired or is no longer valid.")

    # The service role, because there is no authenticated user to bind — and
    # therefore an explicit id lookup and nothing derived from user input
    # beyond the id that was signed.
    async with db.service_session() as service:
        row = await fetch_one(
            service, "select payload from reports where id = %s", (report_id,)
        )
    if row is None:
        raise NotFound("This link has expired or is no longer valid.")

    figures = runner.figures_from_payload(row["payload"] or {})
    return HTMLResponse(render.html(figures))
