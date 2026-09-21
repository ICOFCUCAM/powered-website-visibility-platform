"""Search Console tools.

Every result carries the window it covers. That is not decoration: the
milestone's acceptance criterion is that an answer cites its window, and a
model can only cite what it was told. An uncited comparison is how dashboards
lose arguments.

Totals always come from `gsc_totals_daily`, the reconciliation authority.
Summing the query or page dimensions to produce a site total is the one thing
this codebase never does — Google withholds low-volume rows, so the dimensions
do not add up to the site figure and never will.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from api.adapters.db import fetch_all
from api.ai.tools.base import (
    PERIOD_SCHEMA,
    StrategistScope,
    ToolError,
    clamp,
    tool,
)
from api.repositories.postgres.performance import PerformanceRepository, Totals

ORDERABLE = ("clicks", "impressions", "ctr", "position")

LIMIT_SCHEMA = {
    "type": "integer",
    "description": "Rows to return, 1 to 50. Defaults to 10.",
}


def _window(scope: StrategistScope, period: str | None) -> dict[str, Any]:
    start, end = scope.window(period)
    return {
        "label": period or "28d",
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def _totals(row: dict[str, Any] | None) -> dict[str, Any]:
    totals = Totals.of(row)
    return {
        "clicks": totals.clicks,
        "impressions": totals.impressions,
        "ctr": round(totals.ctr, 4) if totals.ctr is not None else None,
        "position": round(totals.position, 1) if totals.position is not None else None,
    }


def _rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "label": row["label"],
            "clicks": int(row["clicks"] or 0),
            "impressions": int(row["impressions"] or 0),
            "ctr": round(float(row["ctr"]), 4) if row["ctr"] is not None else None,
            "position": round(float(row["position"]), 1)
            if row["position"] is not None
            else None,
        }
        for row in rows
    ]


@tool(
    "get_performance_summary",
    description=(
        "Search Console totals for this website over a window: clicks, "
        "impressions, click-through rate and average position, with the "
        "change against the equivalent window immediately before."
    ),
    step_label="checking your search performance",
    properties={"period": PERIOD_SCHEMA},
)
async def get_performance_summary(
    conn: AsyncConnection, scope: StrategistScope, arguments: dict[str, Any]
) -> dict[str, Any]:
    period = arguments.get("period")
    start, end = scope.window(period)
    prior_start, prior_end = scope.previous_window(period)
    repo = PerformanceRepository(conn)

    now = _totals(await repo.totals(scope.website_id, start, end))
    before = _totals(await repo.totals(scope.website_id, prior_start, prior_end))

    if not now["impressions"] and not before["impressions"]:
        return {
            "period": _window(scope, period),
            "has_data": False,
            "note": (
                "Search Console has no data for this website in this window. "
                "That is not the same as zero traffic — it usually means the "
                "connection is new or the sync has not run."
            ),
        }

    return {
        "period": _window(scope, period),
        "has_data": True,
        "totals": now,
        "previous_period": {
            "start": prior_start.isoformat(),
            "end": prior_end.isoformat(),
            **before,
        },
        "change": {
            "clicks": now["clicks"] - before["clicks"],
            "impressions": now["impressions"] - before["impressions"],
            # Position is left as a raw difference with its sign intact, and
            # the meaning is spelled out, because "position improved by -0.7"
            # is how a model produces a confidently backwards sentence.
            "position": round(now["position"] - before["position"], 1)
            if now["position"] is not None and before["position"] is not None
            else None,
            "position_note": "Negative means the average position improved.",
        },
        "comparable": bool(before["impressions"]),
    }


async def _top(
    conn: AsyncConnection,
    scope: StrategistScope,
    arguments: dict[str, Any],
    which: str,
) -> dict[str, Any]:
    period = arguments.get("period")
    order_by = arguments.get("order_by") or "clicks"
    if order_by not in ORDERABLE:
        raise ToolError(f"order_by must be one of: {', '.join(ORDERABLE)}.")

    start, end = scope.window(period)
    repo = PerformanceRepository(conn)
    method = repo.top_queries if which == "queries" else repo.top_pages
    rows = await method(
        scope.website_id, start, end,
        order_by=order_by, limit=clamp(arguments.get("limit")),
    )
    return {
        "period": _window(scope, period),
        "ordered_by": order_by,
        which: _rows(rows),
        "note": (
            "Google withholds low-volume searches, so these rows do not add "
            "up to the site totals."
            if which == "queries"
            else None
        ),
    }


@tool(
    "get_top_queries",
    description=(
        "The searches this website appears for, as Search Console reports "
        "them. Rows can be ordered by clicks, impressions, ctr or position."
    ),
    step_label="checking your top searches",
    properties={
        "period": PERIOD_SCHEMA,
        "limit": LIMIT_SCHEMA,
        "order_by": {"type": "string", "enum": list(ORDERABLE)},
    },
)
async def get_top_queries(
    conn: AsyncConnection, scope: StrategistScope, arguments: dict[str, Any]
) -> dict[str, Any]:
    return await _top(conn, scope, arguments, "queries")


@tool(
    "get_top_pages",
    description=(
        "The pages of this website that Search Console reports impressions "
        "for. Rows can be ordered by clicks, impressions, ctr or position."
    ),
    step_label="checking your top pages",
    properties={
        "period": PERIOD_SCHEMA,
        "limit": LIMIT_SCHEMA,
        "order_by": {"type": "string", "enum": list(ORDERABLE)},
    },
)
async def get_top_pages(
    conn: AsyncConnection, scope: StrategistScope, arguments: dict[str, Any]
) -> dict[str, Any]:
    return await _top(conn, scope, arguments, "pages")


@tool(
    "get_movers",
    description=(
        "The searches or pages whose clicks changed most against the "
        "equivalent window immediately before. This is the tool for "
        "'why did my traffic change' — it names what moved."
    ),
    step_label="finding what changed",
    properties={
        "period": PERIOD_SCHEMA,
        "limit": LIMIT_SCHEMA,
        "dimension": {"type": "string", "enum": ["query", "page"]},
        "direction": {"type": "string", "enum": ["up", "down"]},
    },
)
async def get_movers(
    conn: AsyncConnection, scope: StrategistScope, arguments: dict[str, Any]
) -> dict[str, Any]:
    period = arguments.get("period")
    dimension = arguments.get("dimension") or "query"
    direction = arguments.get("direction") or "down"
    if dimension not in ("query", "page"):
        raise ToolError("dimension must be 'query' or 'page'.")
    if direction not in ("up", "down"):
        raise ToolError("direction must be 'up' or 'down'.")

    table = "gsc_query_daily" if dimension == "query" else "gsc_page_daily"
    key = "query_hash" if dimension == "query" else "url_hash"
    label = "query" if dimension == "query" else "url"
    start, end = scope.window(period)
    prior_start, prior_end = scope.previous_window(period)

    # A full outer join, because something that appeared or vanished entirely
    # is exactly the kind of mover the question is about, and an inner join
    # would hide both.
    rows = await fetch_all(
        conn,
        f"""
        with now as (
            select {key} as k, min({label}) as label,
                   sum(clicks) as clicks, sum(impressions) as impressions,
                   case when sum(impressions) > 0
                        then sum(position * impressions) / sum(impressions) end
                        as position
              from {table}
             where website_id = %s and date between %s and %s
             group by {key}),
        before as (
            select {key} as k, min({label}) as label,
                   sum(clicks) as clicks, sum(impressions) as impressions,
                   case when sum(impressions) > 0
                        then sum(position * impressions) / sum(impressions) end
                        as position
              from {table}
             where website_id = %s and date between %s and %s
             group by {key})
        select coalesce(now.label, before.label)        as label,
               coalesce(now.clicks, 0)                  as clicks,
               coalesce(before.clicks, 0)               as clicks_before,
               coalesce(now.clicks, 0) - coalesce(before.clicks, 0) as change,
               coalesce(now.impressions, 0)             as impressions,
               now.position                             as position,
               before.position                          as position_before
          from now full outer join before on now.k = before.k
         order by (coalesce(now.clicks, 0) - coalesce(before.clicks, 0))
                  {"desc" if direction == "up" else "asc"}
         limit %s
        """,
        (
            scope.website_id, start, end,
            scope.website_id, prior_start, prior_end,
            clamp(arguments.get("limit")),
        ),
    )

    movers = [
        {
            "label": row["label"],
            "clicks": int(row["clicks"]),
            "clicks_before": int(row["clicks_before"]),
            "change": int(row["change"]),
            "impressions": int(row["impressions"]),
            "position": round(float(row["position"]), 1)
            if row["position"] is not None
            else None,
            "position_before": round(float(row["position_before"]), 1)
            if row["position_before"] is not None
            else None,
        }
        for row in rows
        # Only actual movement in the requested direction. Padding the list
        # with rows that did not move would let "here are your biggest
        # losses" be answered with things that gained.
        if (row["change"] < 0 if direction == "down" else row["change"] > 0)
    ]

    return {
        "period": _window(scope, period),
        "compared_with": {
            "start": prior_start.isoformat(),
            "end": prior_end.isoformat(),
        },
        "dimension": dimension,
        "direction": direction,
        "movers": movers,
        "note": (
            "Nothing moved in that direction in this window."
            if not movers
            else None
        ),
    }


@tool(
    "get_keyword_history",
    description=(
        "The daily clicks, impressions and position for one specific search "
        "phrase, exactly as it appears in Search Console."
    ),
    step_label="looking up that search",
    properties={
        "phrase": {"type": "string", "description": "The search phrase."},
        "period": PERIOD_SCHEMA,
    },
    required=["phrase"],
)
async def get_keyword_history(
    conn: AsyncConnection, scope: StrategistScope, arguments: dict[str, Any]
) -> dict[str, Any]:
    phrase = (arguments.get("phrase") or "").strip()
    if not phrase:
        raise ToolError("phrase is required.")
    period = arguments.get("period")
    start, end = scope.window(period)

    rows = await fetch_all(
        conn,
        """
        select date, clicks, impressions, position
          from gsc_query_daily
         where website_id = %s and query = %s and date between %s and %s
         order by date
        """,
        (scope.website_id, phrase, start, end),
    )
    if not rows:
        return {
            "period": _window(scope, period),
            "phrase": phrase,
            "has_data": False,
            "note": (
                f"Search Console reports nothing for {phrase!r} in this "
                "window. It may be spelled differently, or the site may not "
                "appear for it at all."
            ),
        }

    return {
        "period": _window(scope, period),
        "phrase": phrase,
        "has_data": True,
        "totals": {
            "clicks": sum(int(r["clicks"] or 0) for r in rows),
            "impressions": sum(int(r["impressions"] or 0) for r in rows),
        },
        "days": [
            {
                "date": r["date"].isoformat(),
                "clicks": int(r["clicks"] or 0),
                "impressions": int(r["impressions"] or 0),
                "position": round(float(r["position"]), 1)
                if r["position"] is not None
                else None,
            }
            for r in rows
        ],
    }


@tool(
    "compare_periods",
    description=(
        "Site-wide Search Console totals for two windows side by side, for "
        "questions of the form 'is this month worse than last'."
    ),
    step_label="comparing two periods",
    properties={"a": PERIOD_SCHEMA, "b": PERIOD_SCHEMA},
)
async def compare_periods(
    conn: AsyncConnection, scope: StrategistScope, arguments: dict[str, Any]
) -> dict[str, Any]:
    first = arguments.get("a") or "28d"
    second = arguments.get("b") or "28d"
    repo = PerformanceRepository(conn)

    a_start, a_end = scope.window(first)
    # `b` is the equivalent span immediately before `a`, not a second window
    # anchored to today — otherwise "compare 28d with 28d" would compare a
    # window with itself and report that nothing changed.
    b_start, b_end = scope.previous_window(second)

    return {
        "a": {
            "label": first,
            "start": a_start.isoformat(),
            "end": a_end.isoformat(),
            **_totals(await repo.totals(scope.website_id, a_start, a_end)),
        },
        "b": {
            "label": f"the {second} before",
            "start": b_start.isoformat(),
            "end": b_end.isoformat(),
            **_totals(await repo.totals(scope.website_id, b_start, b_end)),
        },
    }

