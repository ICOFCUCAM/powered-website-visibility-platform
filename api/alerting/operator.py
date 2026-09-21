"""Noticing that the machine is broken, and saying so once.

THE FAILURE MODE OF ALERTING IS NOISE, and the second failure mode is
silence. Both are designed against here:

  A nightly job that fails for four hundred websites produces ONE alert —
  "sync_search_console failed for 400 of 412 websites" — not four hundred.
  Aggregation happens before anything is sent, in `scan`, which is a pure
  read and can be tested without a network.

  An alert that fires once and then goes quiet while the problem continues is
  worse than one that repeats, because silence reads as recovery. So an alert
  is an INCIDENT: opened on a fingerprint, counted while it persists,
  re-notified every few hours, and closed with a recovery notice when the scan
  stops seeing it.

The most important check is the first one. A scheduler that has stopped
dispatching makes every other alert silent too — no runs, no failures, nothing
to report — so "nothing has been claimed in two hours and there are websites
that should have been" is the one condition that catches its own absence.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one
from api.alerting.channels import Alert, DeliveryFailed, Notifier

logger = logging.getLogger("visibility_hub.alerting")

#: How far back a scan looks for failures. A night's work plus room for a late
#: catch-up run.
WINDOW = timedelta(hours=24)

#: How long the scheduler may be quiet before that is itself the alert. Longer
#: than the gap between the last nightly job and the first, so a healthy system
#: that simply has nothing due does not page anybody.
DISPATCH_SILENCE = timedelta(hours=8)

#: How often an unresolved incident is mentioned again. Long enough not to be
#: noise, short enough that a problem cannot go unmentioned for a working day.
RENOTIFY_AFTER = timedelta(hours=6)

#: Below this, a failing job is one customer's problem; at or above it, ours.
BROAD_FAILURE_RATIO = 0.5
BROAD_FAILURE_MINIMUM = 3


@dataclass(frozen=True, slots=True)
class Condition:
    """Something true right now that somebody should know about."""

    kind: str
    severity: str
    title: str
    body: str
    #: What makes this the SAME problem across scans. Never includes a count
    #: or a timestamp, or every scan would open a new incident.
    subject: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.kind}:{self.subject}".encode()).hexdigest()


@dataclass
class Outcome:
    opened: list[str] = field(default_factory=list)
    still_open: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    notified: int = 0
    undeliverable: int = 0


# ---------------------------------------------------------------------------
# The scan: a pure read, aggregated before anything is sent
# ---------------------------------------------------------------------------
async def scan(
    conn: AsyncConnection,
    *,
    now: datetime | None = None,
    window: timedelta = WINDOW,
) -> list[Condition]:
    now = now or datetime.now(UTC)
    since = now - window
    conditions: list[Condition] = []

    conditions += await _scheduler_is_quiet(conn, now=now)
    conditions += await _jobs_failing(conn, since=since)
    conditions += await _crawls_failing(conn, since=since)
    conditions += await _connections_need_reauth(conn)
    return conditions


async def _scheduler_is_quiet(
    conn: AsyncConnection, *, now: datetime
) -> list[Condition]:
    """The check that catches its own absence.

    Guarded on there being websites at all: a fresh install with nothing in it
    has correctly dispatched nothing, and paging somebody about that would
    teach them to ignore the channel on day one.
    """
    row = await fetch_one(
        conn,
        """
        select (select count(*) from websites where archived_at is null) as websites,
               (select max(claimed_at) from scheduled_runs) as last_claim
        """,
    )
    if row is None or not row["websites"]:
        return []

    last = row["last_claim"]
    quiet_for = now - last if last else None
    if last is not None and quiet_for is not None and quiet_for < DISPATCH_SILENCE:
        return []

    hours = round(quiet_for.total_seconds() / 3600, 1) if quiet_for else None
    return [
        Condition(
            kind="scheduler_quiet",
            severity="critical",
            title="The scheduler has stopped dispatching",
            body=(
                f"Nothing has been claimed for {hours} hours"
                if hours is not None
                else "Nothing has ever been claimed"
            )
            + f", and {row['websites']} website(s) are active. "
            "Every other alert is silent while this is true.",
            detail={
                "websites": row["websites"],
                "last_claim": last.isoformat() if last else None,
            },
        )
    ]


async def _jobs_failing(
    conn: AsyncConnection, *, since: datetime
) -> list[Condition]:
    """One condition per job, not one per website.

    Split by the reaper's reason as well as the job, because "no worker picked
    this up" and "the job threw" send an operator to completely different
    places — the first is a pool that is down, the second is a bug.
    """
    rows = await fetch_all(
        conn,
        """
        select job,
               count(*) filter (where status = 'failed') as failed,
               count(*) filter (where status = 'failed'
                                  and error = 'no worker picked this up')
                                                          as unclaimed,
               count(*)                                   as total
          from scheduled_runs
         where window_start >= %s
         group by job
        having count(*) filter (where status = 'failed') > 0
         order by job
        """,
        (since,),
    )

    conditions: list[Condition] = []
    for row in rows:
        failed, total = int(row["failed"]), int(row["total"])
        unclaimed = int(row["unclaimed"])
        broad = (
            failed >= BROAD_FAILURE_MINIMUM
            and failed / total >= BROAD_FAILURE_RATIO
        )
        if unclaimed == failed:
            conditions.append(
                Condition(
                    kind="jobs_unclaimed",
                    subject=row["job"],
                    severity="critical" if broad else "warning",
                    title=f"No worker is running {row['job']}",
                    body=(
                        f"{failed} of {total} scheduled runs were never picked "
                        "up. The pool for this job is down or unreachable."
                    ),
                    detail={"job": row["job"], "unclaimed": failed, "total": total},
                )
            )
            continue

        conditions.append(
            Condition(
                kind="job_failing",
                subject=row["job"],
                severity="critical" if broad else "warning",
                title=f"{row['job']} is failing",
                body=(
                    f"{failed} of {total} runs failed in the last day"
                    + (f", {unclaimed} of them never picked up" if unclaimed else "")
                    + "."
                ),
                detail={"job": row["job"], "failed": failed, "total": total},
            )
        )
    return conditions


async def _crawls_failing(
    conn: AsyncConnection, *, since: datetime
) -> list[Condition]:
    row = await fetch_one(
        conn,
        """
        select count(*) filter (where status = 'failed') as failed,
               count(*) as total
          from crawls where queued_at >= %s
        """,
        (since,),
    )
    if row is None or not row["failed"]:
        return []

    failed, total = int(row["failed"]), int(row["total"])
    broad = failed >= BROAD_FAILURE_MINIMUM and failed / total >= BROAD_FAILURE_RATIO
    return [
        Condition(
            kind="crawls_failing",
            severity="critical" if broad else "warning",
            title="Crawls are failing",
            body=f"{failed} of {total} crawls failed in the last day.",
            detail={"failed": failed, "total": total},
        )
    ]


async def _connections_need_reauth(conn: AsyncConnection) -> list[Condition]:
    """Many at once is our problem, not theirs.

    One customer revoking access is normal and is their business — they get a
    notice of their own. A quarter of the fleet needing re-auth at the same
    time is an OAuth client that has been suspended, a rotated secret, or a
    scope change, and no amount of customer emails fixes it.
    """
    row = await fetch_one(
        conn,
        """
        select count(*) filter (where status = 'needs_reauth') as stale,
               count(*) as total
          from connections where status <> 'revoked'
        """,
    )
    if row is None or not row["total"]:
        return []

    stale, total = int(row["stale"]), int(row["total"])
    if stale < 3 or stale / total < 0.25:
        return []
    return [
        Condition(
            kind="connections_need_reauth",
            severity="critical",
            title="Google connections are failing across the fleet",
            body=(
                f"{stale} of {total} connections need re-authorisation. One "
                "customer is normal; this many at once usually means the "
                "OAuth client, a rotated secret, or a scope change."
            ),
            detail={"needs_reauth": stale, "total": total},
        )
    ]


# ---------------------------------------------------------------------------
# Reconciling: open, re-notify, resolve
# ---------------------------------------------------------------------------
async def reconcile(
    conn: AsyncConnection,
    conditions: list[Condition],
    notifier: Notifier,
    *,
    now: datetime | None = None,
    renotify_after: timedelta = RENOTIFY_AFTER,
) -> Outcome:
    now = now or datetime.now(UTC)
    outcome = Outcome()
    seen = {condition.fingerprint for condition in conditions}

    for condition in conditions:
        row = await fetch_one(
            conn,
            """
            insert into operator_alerts
                (fingerprint, kind, severity, title, detail,
                 first_seen_at, last_seen_at)
            values (%s,%s,%s,%s,%s,%s,%s)
            on conflict (fingerprint) where resolved_at is null do update
               set occurrences = operator_alerts.occurrences + 1,
                   last_seen_at = excluded.last_seen_at,
                   severity = excluded.severity,
                   title = excluded.title,
                   detail = excluded.detail
            returning id, occurrences, notified_at
            """,
            (
                condition.fingerprint,
                condition.kind,
                condition.severity,
                condition.title,
                json.dumps(condition.detail),
                now,
                now,
            ),
        )
        if row is None:  # pragma: no cover - the upsert always returns
            continue

        first_time = row["occurrences"] == 1
        (outcome.opened if first_time else outcome.still_open).append(condition.kind)

        due = row["notified_at"] is None or (
            now - row["notified_at"] >= renotify_after
        )
        if not due:
            continue

        body = condition.body
        if not first_time:
            # Says how long it has been going on. "It happened once at 3am"
            # and "it has been happening all week" are different problems.
            body += f" Seen {row['occurrences']} times since this started."
        await _notify(
            conn, notifier,
            Alert(
                severity=condition.severity,
                title=condition.title,
                body=body,
                kind=condition.kind,
                detail=condition.detail,
            ),
            alert_id=row["id"],
            outcome=outcome,
            now=now,
        )

    # Anything open that this scan no longer sees has recovered.
    open_rows = await fetch_all(
        conn,
        "select id, fingerprint, kind, title, occurrences, first_seen_at"
        "  from operator_alerts where resolved_at is null",
    )
    for row in open_rows:
        if row["fingerprint"] in seen:
            continue
        await conn.execute(
            "update operator_alerts set resolved_at = %s where id = %s",
            (now, row["id"]),
        )
        outcome.resolved.append(row["kind"])
        # A recovery notice only where the problem was announced. Resolving
        # something nobody was told about is not news.
        await _notify(
            conn, notifier,
            Alert(
                severity="recovered",
                title=f"Recovered: {row['title']}",
                body=(
                    "The last scan no longer sees this. It was open for "
                    f"{_duration(now - row['first_seen_at'])} across "
                    f"{row['occurrences']} scan(s)."
                ),
                kind=row["kind"],
            ),
            alert_id=row["id"],
            outcome=outcome,
            now=now,
            resolution=True,
        )

    return outcome


async def _notify(
    conn: AsyncConnection,
    notifier: Notifier,
    alert: Alert,
    *,
    alert_id: int,
    outcome: Outcome,
    now: datetime,
    resolution: bool = False,
) -> None:
    """Deliver, then record. Never the other way round.

    A delivery that failed must not leave `notified_at` set, or the incident
    goes quiet on the next scan having told nobody.
    """
    if resolution:
        told = await fetch_one(
            conn,
            "select notified_at from operator_alerts where id = %s", (alert_id,)
        )
        if told is None or told["notified_at"] is None:
            return

    try:
        await notifier.send(alert)
    except DeliveryFailed as exc:
        logger.warning("alert not delivered: %s", exc)
        outcome.undeliverable += 1
        return

    if not notifier.configured:
        # NullNotifier logged it and nobody received it. Leaving notified_at
        # unset means the day somebody configures a destination, every open
        # incident is announced rather than silently assumed handled.
        outcome.undeliverable += 1
        return

    column = "resolved_notified_at" if resolution else "notified_at"
    await conn.execute(
        f"update operator_alerts"
        f"   set {column} = %s"
        + ("" if resolution else ", notify_count = notify_count + 1")
        + " where id = %s",
        (now, alert_id),
    )
    outcome.notified += 1


def _duration(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 1:
        minutes = int(delta.total_seconds() // 60)
        # "Open for 0 minutes" reads like a bug in the alerting rather than a
        # problem that fixed itself quickly.
        return f"{minutes} minutes" if minutes else "less than a minute"
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


async def run(
    conn: AsyncConnection,
    notifier: Notifier,
    *,
    now: datetime | None = None,
) -> Outcome:
    """One pass: look, then tell."""
    now = now or datetime.now(UTC)
    conditions = await scan(conn, now=now)
    outcome = await reconcile(conn, conditions, notifier, now=now)
    if outcome.opened or outcome.resolved:
        logger.info(
            "alerting: opened=%s resolved=%s notified=%d undeliverable=%d",
            outcome.opened, outcome.resolved, outcome.notified,
            outcome.undeliverable,
        )
    return outcome
