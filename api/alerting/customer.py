"""Telling a customer something only they can fix.

A different audience from `operator.py`, and the distinction is the whole
design. An operator alert says "the machine is broken"; this says "your Google
connection has stopped working, and we cannot reconnect it for you".

The one that matters most is exactly that. A connection in `needs_reauth`
stops every figure on their dashboard from updating — and the dashboard keeps
showing yesterday's numbers, which looks like nothing is wrong. Without a
message they find out in a month, when somebody notices the chart is flat.

TWO RULES, both learned from products that get this wrong:

  **Never tell them about our problems.** A failed sync because Google 500'd,
  a worker that died, a crawl that hit a timeout — those are ours, and go to
  the operator channel. A customer who receives an alert they cannot act on
  learns to ignore the ones they can.

  **Say it once.** `alert_events` records what was sent and when; a condition
  that is still true tomorrow does not produce a second email. Nagging about
  something unresolved teaches people to filter the sender.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all
from api.reports.mail import Mailer, Message
from api.repositories.postgres.organizations import member_emails

logger = logging.getLogger("visibility_hub.alerting")

#: How long the same notice stays suppressed for the same website. Long
#: enough that an unresolved problem is not a weekly nag; short enough that a
#: customer who ignored the first one is reminded before the data gap matters.
REPEAT_AFTER = timedelta(days=14)

#: Consecutive failed crawls before it is worth their attention. One is a
#: blip; three in a row is their server, their robots.txt, or a block.
CRAWL_FAILURES = 3


@dataclass(frozen=True, slots=True)
class Notice:
    organization_id: UUID
    website_id: UUID
    domain: str
    kind: str
    severity: str
    title: str
    body: str
    #: What they should do. Every customer notice has one, because a notice
    #: without an action is just bad news.
    action: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Sent:
    created: list[str] = field(default_factory=list)
    delivered: int = 0
    undeliverable: int = 0
    suppressed: int = 0


async def find(conn: AsyncConnection) -> list[Notice]:
    """Everything a customer needs to know right now."""
    return [*await _connections_need_reauth(conn), *await _crawls_blocked(conn)]


async def _connections_need_reauth(conn: AsyncConnection) -> list[Notice]:
    """Per website, not per connection.

    A connection is an account; a customer thinks in websites. "We've lost
    access to Search Console for example.com" is a sentence they can act on;
    "connection 9f3c…" is not.
    """
    rows = await fetch_all(
        conn,
        """
        select distinct w.id as website_id, w.organization_id, w.domain,
               l.service, c.label as account
          from connections c
          join connection_properties p on p.connection_id = c.id
          join website_connections l on l.property_id = p.id
          join websites w on w.id = l.website_id
         where c.status = 'needs_reauth'
           and l.status = 'active'
           and w.archived_at is null
         order by w.domain, l.service
        """,
    )
    service_names = {
        "search_console": "Search Console",
        "analytics": "Analytics",
    }
    return [
        Notice(
            organization_id=row["organization_id"],
            website_id=row["website_id"],
            domain=str(row["domain"]),
            kind=f"connection_needs_reauth:{row['service']}",
            severity="high",
            title=f"Reconnect Google {service_names.get(row['service'], row['service'])}",
            body=(
                f"Google has stopped accepting our access to "
                f"{service_names.get(row['service'], row['service'])} for "
                f"{row['domain']}. Until it is reconnected, the figures on "
                "your dashboard will stay as they are — they will not go "
                "wrong, but they will stop updating."
            ),
            action="Open Settings and reconnect your Google account.",
            evidence={"service": row["service"], "account": row["account"]},
        )
        for row in rows
    ]


async def _crawls_blocked(conn: AsyncConnection) -> list[Notice]:
    """Their site, not our crawler.

    Only fires when the most recent runs all failed: a crawler that is broken
    for everybody is an operator problem and is reported as one. This is the
    case where the failures are concentrated on one website, which usually
    means their server, their robots.txt, or a block on us.
    """
    rows = await fetch_all(
        conn,
        """
        with recent as (
            select c.website_id, c.status, c.error_summary,
                   row_number() over (partition by c.website_id
                                      order by c.queued_at desc) as rank
              from crawls c
             where c.status in ('completed', 'failed')
        )
        select w.id as website_id, w.organization_id, w.domain,
               count(*) filter (where r.status = 'failed') as failed
          from recent r
          join websites w on w.id = r.website_id
         where r.rank <= %s and w.archived_at is null
         group by w.id, w.organization_id, w.domain
        having count(*) = %s
           and count(*) filter (where r.status = 'failed') = %s
         order by w.domain
        """,
        (CRAWL_FAILURES, CRAWL_FAILURES, CRAWL_FAILURES),
    )
    return [
        Notice(
            organization_id=row["organization_id"],
            website_id=row["website_id"],
            domain=str(row["domain"]),
            kind="crawl_failing",
            severity="high",
            title=f"We can't scan {row['domain']}",
            body=(
                f"Our last {CRAWL_FAILURES} attempts to scan {row['domain']} "
                "all failed. Your Google figures are unaffected, but the "
                "page-level findings and your score are frozen at the last "
                "successful scan."
            ),
            action=(
                "Check the site is reachable and that robots.txt does not "
                "block us, then start a scan from your dashboard."
            ),
            evidence={"consecutive_failures": int(row["failed"])},
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Recording and delivery
# ---------------------------------------------------------------------------
async def deliver(
    conn: AsyncConnection,
    notices: list[Notice],
    mailer: Mailer | None,
    *,
    now: datetime | None = None,
    repeat_after: timedelta = REPEAT_AFTER,
    base_url: str = "",
) -> Sent:
    now = now or datetime.now(UTC)
    sent = Sent()

    for notice in notices:
        recent = await fetch_all(
            conn,
            "select id from alert_events"
            " where website_id = %s and kind = %s and fired_at >= %s limit 1",
            (notice.website_id, notice.kind, now - repeat_after),
        )
        if recent:
            sent.suppressed += 1
            continue

        row = await fetch_all(
            conn,
            """
            insert into alert_events (organization_id, website_id, kind,
                                      severity, title, evidence, fired_at)
            values (%s,%s,%s,%s,%s,%s,%s)
            returning id
            """,
            (
                notice.organization_id,
                notice.website_id,
                notice.kind,
                notice.severity,
                notice.title,
                json.dumps(notice.evidence),
                now,
            ),
        )
        event_id = row[0]["id"]
        sent.created.append(notice.kind)

        recipients = await member_emails(conn, notice.organization_id)
        if mailer is None or not recipients:
            # Recorded but not delivered. `delivered_at` stays null and the
            # reason is written down, so "we told them" is never assumed from
            # the existence of the row.
            await conn.execute(
                "update alert_events set delivery_error = %s where id = %s",
                (
                    "no mail server configured" if mailer is None
                    else "no recipients",
                    event_id,
                ),
            )
            sent.undeliverable += 1
            continue

        try:
            await mailer.send(_message(notice, recipients, base_url))
        except Exception as exc:
            logger.warning("customer alert not delivered: %s", exc)
            await conn.execute(
                "update alert_events set delivery_error = %s where id = %s",
                (f"{type(exc).__name__}: {exc}"[:500], event_id),
            )
            sent.undeliverable += 1
            continue

        await conn.execute(
            "update alert_events set delivered_at = %s, delivery_error = null"
            " where id = %s",
            (now, event_id),
        )
        sent.delivered += 1

    return sent


def _message(notice: Notice, recipients: list[str], base_url: str) -> Message:
    link = f"\n\n{base_url}/settings" if base_url else ""
    text = f"{notice.body}\n\nWhat to do: {notice.action}{link}"
    return Message(
        to=recipients,
        subject=f"{notice.title} — {notice.domain}",
        text=text,
        html=(
            '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,'
            'sans-serif;font-size:15px;line-height:1.6;max-width:60ch;">'
            f"<p>{notice.body}</p>"
            f"<p><strong>What to do:</strong> {notice.action}</p>"
            + (
                f'<p><a href="{base_url}/settings">Open settings</a></p>'
                if base_url
                else ""
            )
            + "</div>"
        ),
    )


async def run(
    conn: AsyncConnection,
    mailer: Mailer | None,
    *,
    now: datetime | None = None,
    base_url: str = "",
) -> Sent:
    return await deliver(conn, await find(conn), mailer, now=now, base_url=base_url)
