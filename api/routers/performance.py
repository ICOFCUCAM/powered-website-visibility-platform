"""Search performance (V1 spec s19, s30).

Reads Postgres only. Nothing in this path touches a Google API, which is what
makes the dashboard survive a Google outage and what keeps quota off the
page-load path.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from api.deps import ConnectionDep, WebsiteScopeDep
from api.repositories.postgres.performance import (
    AnalyticsRepository,
    PerformanceRepository,
    Totals,
)

router = APIRouter(prefix="/websites", tags=["performance"])

DEFAULT_WINDOW_DAYS = 28

#: Search Console lags ~3 days. Defaulting a window to "today" would always
#: show an empty tail and look like a broken chart.
REPORTING_LAG_DAYS = 3


class TotalsOut(BaseModel):
    clicks: int
    impressions: int
    ctr: float | None
    position: float | None


class DeltaOut(BaseModel):
    clicks: float | None
    impressions: float | None
    ctr: float | None
    position: float | None


class PointOut(BaseModel):
    date: date
    clicks: int
    impressions: int
    ctr: float | None
    position: float | None


class AnonymisedOut(BaseModel):
    total_clicks: int
    attributed_clicks: int
    anonymised_clicks: int
    anonymised_share: float | None
    note: str


class PerformanceOut(BaseModel):
    start: date
    end: date
    totals: TotalsOut
    compared_to: TotalsOut | None
    delta: DeltaOut | None
    series: list[PointOut]
    anonymised: AnonymisedOut


class RowOut(BaseModel):
    label: str
    clicks: int
    impressions: int
    ctr: float | None
    position: float | None


class RowsOut(BaseModel):
    start: date
    end: date
    rows: list[RowOut]
    anonymised: AnonymisedOut


def _default_window() -> tuple[date, date]:
    end = date.today() - timedelta(days=REPORTING_LAG_DAYS)
    return end - timedelta(days=DEFAULT_WINDOW_DAYS - 1), end


def _pct(now: float | None, before: float | None) -> float | None:
    if now is None or before in (None, 0):
        return None
    return round((now - before) / before * 100, 2)


def _anonymised(row) -> AnonymisedOut:
    share = row.get("anonymised_share") if row else None
    return AnonymisedOut(
        total_clicks=int((row or {}).get("total_clicks") or 0),
        attributed_clicks=int((row or {}).get("attributed_clicks") or 0),
        anonymised_clicks=int((row or {}).get("anonymised_clicks") or 0),
        anonymised_share=float(share) if share is not None else None,
        note=(
            "Google withholds low-volume queries, so the rows below do not add "
            "up to the site totals. The difference is shown as anonymised."
        ),
    )


@router.get("/{website_id}/search-performance", response_model=PerformanceOut)
async def search_performance(
    scope: WebsiteScopeDep,
    conn: ConnectionDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
    compare: Annotated[bool, Query()] = True,
) -> PerformanceOut:
    start, end = (from_, to) if from_ and to else _default_window()
    repo = PerformanceRepository(conn)

    totals = Totals.of(await repo.totals(scope.website.id, start, end))
    series = await repo.daily_series(scope.website.id, start, end)
    anonymised = await repo.anonymised_share(scope.website.id, start, end)

    previous = delta = None
    if compare:
        span = (end - start).days + 1
        prior_end = start - timedelta(days=1)
        previous = Totals.of(
            await repo.totals(
                scope.website.id, prior_end - timedelta(days=span - 1), prior_end
            )
        )
        delta = DeltaOut(
            clicks=_pct(totals.clicks, previous.clicks),
            impressions=_pct(totals.impressions, previous.impressions),
            ctr=_pct(totals.ctr, previous.ctr),
            # Position improving means the number going DOWN, so the delta is
            # inverted here rather than in every chart that renders it.
            position=_pct(previous.position, totals.position),
        )

    return PerformanceOut(
        start=start,
        end=end,
        totals=TotalsOut(**asdict(totals)),
        compared_to=TotalsOut(**asdict(previous)) if previous else None,
        delta=delta,
        series=[PointOut(**p) for p in series],
        anonymised=_anonymised(anonymised),
    )


async def _rows(
    scope, conn, from_, to, order_by, limit, kind: Literal["queries", "pages"]
) -> RowsOut:
    start, end = (from_, to) if from_ and to else _default_window()
    repo = PerformanceRepository(conn)
    fetch = repo.top_queries if kind == "queries" else repo.top_pages
    rows = await fetch(scope.website.id, start, end, order_by=order_by, limit=limit)
    return RowsOut(
        start=start,
        end=end,
        rows=[RowOut(**r) for r in rows],
        anonymised=_anonymised(await repo.anonymised_share(scope.website.id, start, end)),
    )


@router.get("/{website_id}/queries", response_model=RowsOut)
async def queries(
    scope: WebsiteScopeDep,
    conn: ConnectionDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
    order_by: Annotated[
        Literal["clicks", "impressions", "ctr", "position"], Query()
    ] = "clicks",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RowsOut:
    return await _rows(scope, conn, from_, to, order_by, limit, "queries")


@router.get("/{website_id}/pages", response_model=RowsOut)
async def pages(
    scope: WebsiteScopeDep,
    conn: ConnectionDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
    order_by: Annotated[
        Literal["clicks", "impressions", "ctr", "position"], Query()
    ] = "clicks",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RowsOut:
    return await _rows(scope, conn, from_, to, order_by, limit, "pages")


class OutcomeOut(BaseModel):
    event_name: str
    label: str
    goal_kind: str
    count: int


class AnalyticsOut(BaseModel):
    start: date
    end: date
    sessions: int
    active_users: int
    engaged_sessions: int
    engagement_rate: float | None
    # Deliberately nullable rather than 0: "we don't know" and "none happened"
    # are different answers, and conflating them invents a conversion rate.
    outcomes_configured: bool
    outcomes: list[OutcomeOut]
    outcomes_note: str | None
    top_channels: list[dict]
    top_countries: list[dict]
    top_devices: list[dict]


@router.get("/{website_id}/analytics", response_model=AnalyticsOut)
async def analytics(
    scope: WebsiteScopeDep,
    conn: ConnectionDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
) -> AnalyticsOut:
    start, end = (from_, to) if from_ and to else _default_window()
    repo = AnalyticsRepository(conn)

    totals = await repo.totals(scope.website.id, start, end) or {}
    configured = await repo.has_goals(scope.website.id)
    outcomes = await repo.outcomes(scope.website.id, start, end) if configured else []

    return AnalyticsOut(
        start=start,
        end=end,
        sessions=int(totals.get("sessions") or 0),
        active_users=int(totals.get("active_users") or 0),
        engaged_sessions=int(totals.get("engaged_sessions") or 0),
        engagement_rate=(
            float(totals["engagement_rate"])
            if totals.get("engagement_rate") is not None
            else None
        ),
        outcomes_configured=configured,
        outcomes=[
            OutcomeOut(
                event_name=o["event_name"],
                label=o["label"],
                goal_kind=o["goal_kind"],
                count=int(o["count"]),
            )
            for o in outcomes
        ],
        outcomes_note=(
            None
            if configured
            else (
                "Tell us which Analytics event means an enquiry or a sale and "
                "we'll show outcomes here. We can't work it out from the name."
            )
        ),
        top_channels=[
            dict(r)
            for r in await repo.by_dimension(
                scope.website.id, "session_default_channel_group", start, end, 8
            )
        ],
        top_countries=[
            dict(r)
            for r in await repo.by_dimension(scope.website.id, "country", start, end, 8)
        ],
        top_devices=[
            dict(r)
            for r in await repo.by_dimension(
                scope.website.id, "device_category", start, end, 5
            )
        ],
    )
