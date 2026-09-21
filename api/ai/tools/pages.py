"""One page, everything known about it.

The URL arrives from the model, which means it arrives from a conversation,
which means it can be anything. It is matched against `pages` for THIS website
only — by exact URL, then by path — so an absolute URL pointing at another
domain simply does not match rather than reaching anything.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one
from api.ai.tools.base import PERIOD_SCHEMA, StrategistScope, ToolError, tool

HISTORY_DAYS = 30


def _path_of(value: str) -> str:
    """A path the customer's own pages table could hold.

    The model is as likely to say "/sourdough" as the full URL, and a customer
    quoting a page in chat will paste either.
    """
    parsed = urlsplit(value)
    path = parsed.path or "/"
    return path if path.startswith("/") else f"/{path}"


@tool(
    "get_page_detail",
    description=(
        "Everything known about one page of this website: what the last crawl "
        "found on it, the problems open against it, and its Search Console "
        "history. Accepts a full URL or a path like /about."
    ),
    step_label="looking up that page",
    properties={
        "url": {"type": "string", "description": "Full URL or path."},
        "period": PERIOD_SCHEMA,
    },
    required=["url"],
)
async def get_page_detail(
    conn: AsyncConnection, scope: StrategistScope, arguments: dict[str, Any]
) -> dict[str, Any]:
    raw = (arguments.get("url") or "").strip()
    if not raw:
        raise ToolError("url is required.")

    page = await fetch_one(
        conn,
        """
        select id, url, path, last_status, is_indexable, last_seen_at
          from pages
         where website_id = %s and (url = %s or path = %s)
         order by (url = %s) desc
         limit 1
        """,
        (scope.website_id, raw, _path_of(raw), raw),
    )
    if page is None:
        return {
            "url": raw,
            "found": False,
            "note": (
                f"No page at {raw!r} on {scope.domain}. It may never have been "
                "crawled, or it may belong to a different website."
            ),
        }

    snapshot = await fetch_one(
        conn,
        """
        select fetched_at, status_code, title, meta_description, h1,
               word_count, canonical_url, canonical_is_self, robots_meta,
               schema_types, images_total, images_missing_alt,
               internal_inlinks, internal_outlinks, render_mode
          from page_snapshots
         where page_id = %s
         order by fetched_at desc
         limit 1
        """,
        (page["id"],),
    )

    issues = await fetch_all(
        conn,
        """
        select t.key as type_key, t.title, i.severity, i.impact_score,
               i.evidence
          from issues i join issue_types t on t.key = i.type_key
         where i.page_id = %s and i.status in ('open', 'regressed')
         order by i.impact_score desc
         limit 20
        """,
        (page["id"],),
    )

    start, end = scope.window(arguments.get("period"))
    performance = await fetch_one(
        conn,
        """
        select coalesce(sum(clicks), 0) as clicks,
               coalesce(sum(impressions), 0) as impressions,
               case when sum(impressions) > 0
                    then sum(position * impressions) / sum(impressions) end
                    as position
          from gsc_page_daily
         where website_id = %s and url = %s and date between %s and %s
        """,
        (scope.website_id, page["url"], start, end),
    )

    return {
        "url": page["url"],
        "found": True,
        "last_crawled": page["last_seen_at"].date().isoformat()
        if page["last_seen_at"]
        else None,
        "status_code": page["last_status"],
        "indexable": page["is_indexable"],
        "content": _snapshot(snapshot),
        "open_issues": [
            {
                "type": row["type_key"],
                "title": row["title"],
                "severity": row["severity"],
                "estimated_monthly_clicks": round(float(row["impact_score"] or 0)),
            }
            for row in issues
        ],
        "search": {
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "clicks": int((performance or {}).get("clicks") or 0),
            "impressions": int((performance or {}).get("impressions") or 0),
            "position": round(float(performance["position"]), 1)
            if performance and performance["position"] is not None
            else None,
        },
    }


def _snapshot(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "fetched_at": row["fetched_at"].isoformat() if row["fetched_at"] else None,
        "status_code": row["status_code"],
        "title": row["title"],
        "meta_description": row["meta_description"],
        "h1": list(row["h1"] or []),
        "word_count": row["word_count"],
        "canonical_url": row["canonical_url"],
        "canonical_points_here": row["canonical_is_self"],
        "robots_meta": list(row["robots_meta"] or []),
        "structured_data": list(row["schema_types"] or []),
        "images": {
            "total": row["images_total"],
            "missing_alt": row["images_missing_alt"],
        },
        "internal_links_in": row["internal_inlinks"],
        "internal_links_out": row["internal_outlinks"],
        "rendered_with": row["render_mode"],
    }
