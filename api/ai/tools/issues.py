"""Findings, as the rules engine recorded them.

The Strategist reads issues; it never creates one. A problem exists because a
deterministic rule observed it on a crawled page, and the model's job is to
explain and prioritise that evidence, not to add to it.

`impact_score` is estimated additional monthly clicks, computed in code. It is
handed over already ranked so the model has no reason to invent an ordering of
its own.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from api.adapters.db import fetch_all
from api.ai.tools.base import StrategistScope, ToolError, clamp, tool

CATEGORIES = ("technical", "content", "seo", "authority", "ai_search")


@tool(
    "get_open_issues",
    description=(
        "The problems currently detected on this website, ranked by estimated "
        "additional monthly clicks. Optionally filtered to one category: "
        "technical, content, seo, authority or ai_search."
    ),
    step_label="checking what's wrong with the site",
    properties={
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "limit": {"type": "integer", "description": "1 to 50. Defaults to 10."},
    },
)
async def get_open_issues(
    conn: AsyncConnection, scope: StrategistScope, arguments: dict[str, Any]
) -> dict[str, Any]:
    category = arguments.get("category")
    if category is not None and category not in CATEGORIES:
        raise ToolError(f"category must be one of: {', '.join(CATEGORIES)}.")

    rows = await fetch_all(
        conn,
        """
        select t.key as type_key, t.category, t.title, t.summary, t.effort,
               i.severity,
               count(*) as pages_affected,
               sum(i.impact_score) as estimated_monthly_clicks,
               min(i.first_detected_at) as first_detected_at,
               array_remove(
                   array_agg(
                       coalesce(p.url, i.evidence->>'url', i.evidence->>'query')
                       order by i.impact_score desc, i.id),
                   null) as examples
          from issues i
          join issue_types t on t.key = i.type_key
          left join pages p on p.id = i.page_id
         where i.website_id = %s
           and i.status in ('open', 'regressed')
           and (%s::text is null or t.category = %s)
         group by t.key, t.category, t.title, t.summary, t.effort, i.severity
         order by sum(i.impact_score) desc, count(*) desc, t.key
         limit %s
        """,
        (scope.website_id, category, category, clamp(arguments.get("limit"))),
    )

    return {
        "issues": [
            {
                "type": row["type_key"],
                "category": row["category"],
                "title": row["title"],
                "what_it_means": row["summary"],
                "severity": row["severity"],
                "pages_affected": int(row["pages_affected"]),
                "estimated_monthly_clicks": round(
                    float(row["estimated_monthly_clicks"] or 0)
                ),
                "effort": row["effort"],
                "first_detected": row["first_detected_at"].date().isoformat(),
                "examples": list(row["examples"] or [])[:5],
            }
            for row in rows
        ],
        "ranked_by": "estimated additional monthly clicks, computed in code",
        "note": (
            "No problems are currently open on this website."
            if not rows
            else None
        ),
    }
