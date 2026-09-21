"""What has changed on this website, and how fresh any of this is.

The freshness half matters as much as the changes. "Your traffic fell" is a
different sentence if the last crawl was yesterday than if it was in March,
and a model with no way to know which will say it with equal confidence.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one
from api.ai.tools.base import PERIOD_SCHEMA, StrategistScope, clamp, tool


@tool(
    "get_site_changes",
    description=(
        "What changed on this website recently: problems that were fixed, "
        "problems that came back, fixes that were verified, and when the "
        "website was last crawled and last synced with Google."
    ),
    step_label="checking what changed on the site",
    properties={
        "period": PERIOD_SCHEMA,
        "limit": {"type": "integer", "description": "1 to 50. Defaults to 20."},
    },
)
async def get_site_changes(
    conn: AsyncConnection, scope: StrategistScope, arguments: dict[str, Any]
) -> dict[str, Any]:
    start, end = scope.window(arguments.get("period"))

    observations = await fetch_all(
        conn,
        """
        select o.observed_at, o.present, i.status, t.key as type_key, t.title,
               coalesce(p.url, i.evidence->>'url', i.evidence->>'query') as target
          from issue_observations o
          join issues i on i.id = o.issue_id
          join issue_types t on t.key = i.type_key
          left join pages p on p.id = i.page_id
         where o.website_id = %s
           and o.observed_at >= %s
           and (o.present = false or i.status in ('regressed', 'verified'))
         order by o.observed_at desc
         limit %s
        """,
        (scope.website_id, start, clamp(arguments.get("limit"), default=20)),
    )

    crawl = await fetch_one(
        conn,
        """
        select finished_at, pages_fetched, status
          from crawls
         where website_id = %s and status in ('completed', 'failed', 'cancelled')
         order by queued_at desc limit 1
        """,
        (scope.website_id,),
    )
    search_through = await fetch_one(
        conn,
        "select max(date) as through from gsc_totals_daily where website_id = %s",
        (scope.website_id,),
    )

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "changes": [
            {
                "when": row["observed_at"].date().isoformat(),
                "what": (
                    "fixed"
                    if not row["present"]
                    else "came back"
                    if row["status"] == "regressed"
                    else "confirmed fixed"
                ),
                "problem": row["title"],
                "type": row["type_key"],
                "target": row["target"],
            }
            for row in observations
        ],
        # Said explicitly so the model can qualify an answer rather than
        # asserting a stale figure with a fresh tone.
        "freshness": {
            "last_crawled": (crawl or {}).get("finished_at").date().isoformat()
            if crawl and crawl.get("finished_at")
            else None,
            "pages_crawled": (crawl or {}).get("pages_fetched"),
            "last_crawl_status": (crawl or {}).get("status"),
            "search_data_through": (search_through or {}).get("through").isoformat()
            if search_through and search_through.get("through")
            else None,
        },
        "note": (
            "Nothing has been fixed or come back in this window."
            if not observations
            else None
        ),
    }
