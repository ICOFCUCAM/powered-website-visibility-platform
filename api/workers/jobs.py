"""The six nightly jobs (V1 spec §31).

Thin on purpose. Every one of these is the same four steps — take the run,
do the work with a service connection, record what happened, release — and
the work itself is the code that already existed and is already tested:
`sync_website`, `CrawlRunner`, `AnalysisRunner`, `WeeklyPlanService`,
`reports.runner`. A scheduler that reimplemented any of them would be a second
definition of the product's behaviour, drifting quietly out of step with the
one the API exercises.

Two properties every job here has:

  **It refuses a second start.** `mark_running` flips `claimed` to `running`
  and returns nothing if the row has already moved on, so a broker redelivery
  or a retry after a worker was killed cannot run a crawl twice.

  **A failure is recorded, not raised into the void.** The run row ends
  `failed` with its reason, so "did this customer's data refresh last night?"
  has an answer that is not a log search.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from api.adapters import db
from api.adapters.db import fetch_one
from api.ai.deps import get_provider
from api.ai.plan import WeeklyPlanService
from api.alerting import channels, customer, operator
from api.analysis.runner import AnalysisRunner
from api.crawler.fetch import Fetcher
from api.crawler.politeness import HostLimiter
from api.crawler.runner import CrawlRunner
from api.crawler.storage import ArtifactStore, LocalArtifactStore
from api.hub.services.sync.orchestrator import Skipped, sync_website
from api.reports import runner as reports
from api.reports.mail import SmtpMailer, SmtpSettings
from api.workers.dispatch import finish, mark_running

logger = logging.getLogger("visibility_hub.workers")

#: Where fetched HTML goes. Object storage is the system of record for raw
#: artifacts (docs/03-crawler.md); this is the local stand-in until the
#: bucket exists, and it is a real directory rather than a temp one because
#: `page_snapshots.raw_key` points at it.
ARTIFACT_ROOT = Path(os.environ.get("ARTIFACT_ROOT", "var/artifacts"))


def artifact_store() -> ArtifactStore:
    return LocalArtifactStore(ARTIFACT_ROOT)


async def _run_context(conn: AsyncConnection, run_id: int) -> dict[str, Any] | None:
    """The claimed run, plus everything about the website the job will need."""
    started = await mark_running(conn, run_id)
    if started is None:
        # Two very different situations, and conflating them is how the
        # commit-ordering bug above stayed invisible: a row that has moved on
        # is an ordinary redelivery, a row that is not there at all is a bug
        # in whatever enqueued this.
        exists = await fetch_one(
            conn, "select status from scheduled_runs where id = %s", (run_id,)
        )
        if exists is None:
            logger.error(
                "run %s does not exist: a task was queued before its claim "
                "was committed",
                run_id,
            )
        else:
            logger.info(
                "run %s is already %s; not running it twice",
                run_id, exists["status"],
            )
        return None

    website = await fetch_one(
        conn,
        """
        select w.id, w.organization_id, w.domain, w.canonical_url, w.crawl_config,
               o.max_pages_per_crawl
          from websites w join organizations o on o.id = w.organization_id
         where w.id = %s and w.archived_at is null
        """,
        (started["website_id"],),
    )
    if website is None:
        await finish(conn, run_id, status="skipped", error="website is gone")
        return None
    return {**started, "website": website}


async def _guard(run_id: int, job: str, body) -> dict[str, Any]:
    """Open a service connection, run the body, record the outcome.

    The service role, because this work spans organisations and has no user to
    bind — so every query inside carries its own explicit tenant value, taken
    from the claimed run rather than from anything a client sent.

    Autocommit (`service_task`, not `service_session`): a crawl must not be
    one long transaction, and the row recording a failure must not be rolled
    back by the failure it is recording.
    """
    async with db.service_task() as conn:
        context = await _run_context(conn, run_id)
        if context is None:
            return {"status": "skipped"}

        try:
            detail = await body(conn, context) or {}
        except Exception as exc:
            logger.exception("%s failed for run %s", job, run_id)
            await finish(
                conn, run_id, status="failed", error=f"{type(exc).__name__}: {exc}"
            )
            raise

        status = "skipped" if detail.pop("_skipped", False) else "succeeded"
        await finish(conn, run_id, status=status, detail=detail)
        return {"status": status, **detail}


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------
# No client, no vault, no token: the worker asks the Hub to sync a website and
# the Hub builds, uses and discards its own Google client. That is the whole
# of this module's relationship with Google, and pyproject's import contract
# forbids any other.
async def _sync(run_id: int, service: str) -> dict[str, Any]:
    async def body(conn, context):
        outcome = await sync_website(
            conn, website_id=context["website"]["id"], service=service
        )
        if isinstance(outcome, Skipped):
            return {"_skipped": True, "reason": outcome.reason}
        return {
            "rows_written": outcome.rows_written,
            "api_calls": outcome.api_calls,
            "quota_hits": outcome.quota_hits,
            "sync_status": outcome.status,
        }

    return await _guard(run_id, f"sync_{service}", body)


async def sync_search_console(run_id: int) -> dict[str, Any]:
    return await _sync(run_id, "search_console")


async def sync_analytics(run_id: int) -> dict[str, Any]:
    return await _sync(run_id, "analytics")


# ---------------------------------------------------------------------------
# The crawl
# ---------------------------------------------------------------------------
async def crawl_website(
    run_id: int,
    *,
    store: ArtifactStore | None = None,
    fetcher: Fetcher | None = None,
    limiter: HostLimiter | None = None,
) -> dict[str, Any]:
    """The collaborators are injectable for the same reason they are on
    `CrawlRunner`: a crawl that can only be exercised against the open web
    cannot be exercised at all."""

    async def body(conn, context):
        website = context["website"]
        config = website["crawl_config"] or {}

        row = await fetch_one(
            conn,
            "insert into crawls (organization_id, website_id, trigger, status) "
            "values (%s,%s,'scheduled','queued') returning id",
            (website["organization_id"], website["id"]),
        )
        host_limiter = limiter or HostLimiter()
        summary = await CrawlRunner(
            conn,
            fetcher=fetcher or Fetcher(host_limiter),
            store=store or artifact_store(),
            limiter=host_limiter,
            worker_id=f"scheduler:{run_id}",
        ).run(
            crawl_id=row["id"],
            organization_id=website["organization_id"],
            website_id=website["id"],
            origin=website["canonical_url"],
            max_pages=int(website["max_pages_per_crawl"]),
            include_subdomains=bool(config.get("include_subdomains", False)),
        )
        return {
            "crawl_id": str(row["id"]),
            "pages_fetched": summary.fetched,
            "pages_discovered": summary.discovered,
            "fetch_errors": summary.errors,
            # Recorded because a truncated crawl that looks complete produces
            # analysis that is quietly wrong (docs/03-crawler.md).
            "hit_page_cap": summary.hit_page_cap,
        }

    return await _guard(run_id, "crawl_website", body)


# ---------------------------------------------------------------------------
# Analysis and the plan
# ---------------------------------------------------------------------------
async def calculate_scores(run_id: int) -> dict[str, Any]:
    """Re-runs the rules over the latest crawl.

    Nightly rather than only after a crawl, because half the rules read Search
    Console: a site whose crawl has not changed can still acquire a
    below-baseline CTR finding overnight, and a score that only moved when
    someone re-crawled would be a chart of our scheduling.
    """

    async def body(conn, context):
        website = context["website"]
        result = await AnalysisRunner(conn).run(
            organization_id=website["organization_id"],
            website_id=website["id"],
            crawl_id=None,
        )
        return {
            "issues_open": result.issues_open,
            "issues_new": result.issues_new,
            "issues_resolved": result.issues_resolved,
            "score": result.score_total,
        }

    return await _guard(run_id, "calculate_scores", body)


async def generate_recommendations(run_id: int) -> dict[str, Any]:
    async def body(conn, context):
        website = context["website"]
        plan = await WeeklyPlanService(
            conn,
            organization_id=website["organization_id"],
            website_id=website["id"],
            provider=get_provider(),
        ).generate()
        return {
            "plan_id": str(plan.plan_id),
            "priorities": len(plan.priorities),
            "fallback_reason": plan.fallback_reason,
        }

    return await _guard(run_id, "generate_recommendations", body)


async def generate_weekly_report(run_id: int) -> dict[str, Any]:
    """Builds the report, and sends it only if email is actually configured.

    A generated draft nobody receives is a reasonable state to be in; a row
    marked sent when no mail server exists is not."""

    async def body(conn, context):
        website = context["website"]
        result = await reports.generate(
            conn,
            organization_id=website["organization_id"],
            website_id=website["id"],
            provider=get_provider(),
            store=artifact_store(),
            dashboard_url=f"{os.environ.get('WEB_BASE_URL', '')}/dashboard".lstrip("/"),
        )
        detail: dict[str, Any] = {
            "report_id": str(result.report_id),
            "week_start": result.figures.week_start.isoformat(),
            "sent": False,
        }

        smtp = SmtpSettings.from_env()
        if smtp is None or result.status == "sent":
            return detail

        recipients = await reports.recipients_for(conn, website["organization_id"])
        if not recipients:
            return detail

        await reports.send(
            conn,
            report_id=result.report_id,
            mailer=SmtpMailer(smtp),
            recipients=recipients,
            store=artifact_store(),
        )
        detail["sent"] = True
        detail["recipients"] = len(recipients)
        return detail

    return await _guard(run_id, "generate_weekly_report", body)


# ---------------------------------------------------------------------------
# Housekeeping, which is not per-website
# ---------------------------------------------------------------------------
async def ensure_partitions(months: int = 3) -> dict[str, Any]:
    """A missing partition is an INSERT failure, not a silent drop.

    Runs daily and stays three months ahead, so the fact tables never meet a
    month nobody created. Listed as the scheduler's job in
    docs/08-architecture.md, and it is the one piece of the night that has
    nothing to do with any particular customer.
    """
    async with db.service_task() as conn:
        await conn.execute("select app.ensure_partitions_ahead(%s)", (months,))
    logger.info("partitions ensured %d months ahead", months)
    return {"months": months, "at": datetime.now(UTC).isoformat()}


async def alerts() -> dict[str, Any]:
    """Look at the fleet, tell whoever needs to know.

    Runs on its own cadence rather than inside the dispatcher's tick: the tick
    is every five minutes because a slot should be claimed promptly, and
    scanning for problems that often would mean either noise or a scan that
    does nothing 95% of the time. Suppression makes the exact cadence
    unimportant — a condition that is still true is not announced twice — so
    the interval is chosen for cost, not for correctness.
    """
    notifier = channels.from_env()
    smtp = SmtpSettings.from_env()
    mailer = SmtpMailer(smtp) if smtp else None

    async with db.service_task() as conn:
        fleet = await operator.run(conn, notifier)
        theirs = await customer.run(
            conn, mailer, base_url=os.environ.get("WEB_BASE_URL", "")
        )

    return {
        "opened": fleet.opened,
        "resolved": fleet.resolved,
        "notified": fleet.notified,
        "undeliverable": fleet.undeliverable + theirs.undeliverable,
        "customer_notices": len(theirs.created),
        "customer_delivered": theirs.delivered,
        "customer_suppressed": theirs.suppressed,
    }


BY_NAME = {
    "sync_search_console": sync_search_console,
    "sync_analytics": sync_analytics,
    "crawl_website": crawl_website,
    "calculate_scores": calculate_scores,
    "generate_recommendations": generate_recommendations,
    "generate_weekly_report": generate_weekly_report,
}
