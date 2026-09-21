"""The home screen (V1 spec s18, s30).

One call. Every number carries enough context to be read honestly: a score
with no history says so rather than showing a 0% change, a component with no
data source is absent rather than zero, and the sliced Google figures carry
the anonymised-clicks note.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from api.deps import ConnectionDep, WebsiteScopeDep
from api.repositories.postgres.dashboard import (
    DashboardRepository,
    pct_change,
    phrase_for,
)
from api.repositories.postgres.performance import (
    AnalyticsRepository,
    PerformanceRepository,
    Totals,
)

router = APIRouter(prefix="/websites", tags=["dashboard"])

WINDOW_DAYS = 28
GSC_LAG_DAYS = 3
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")
MAX_OPPORTUNITIES = 4


class ScoreOut(BaseModel):
    total: float
    as_of: date
    components: dict[str, float | None]
    # Nullable on purpose: a first measurement has nothing to compare against,
    # and "▲0%" is a claim we cannot support.
    change_pct: float | None
    compared_to: date | None
    is_first_measurement: bool


class AttentionOut(BaseModel):
    critical: int
    content_opportunities: int
    losing_visibility: int
    gaining_visibility: int


class OpportunityOut(BaseModel):
    type_key: str
    headline: str
    count: int
    estimated_clicks: int
    severity: str
    category: str


class SearchOut(BaseModel):
    clicks: int
    impressions: int
    ctr: float | None
    position: float | None
    change: dict[str, float | None] | None
    anonymised_clicks: int
    note: str


class AnalyticsOut(BaseModel):
    connected: bool
    sessions: int
    active_users: int
    engagement_rate: float | None
    outcomes_configured: bool


class ChangeOut(BaseModel):
    observed_at: datetime
    kind: str
    title: str
    url: str | None


class FreshnessOut(BaseModel):
    last_crawl_at: datetime | None
    pages_crawled: int | None
    search_data_through: date | None
    stale: bool


class DashboardOut(BaseModel):
    website: dict[str, Any]
    score: ScoreOut | None
    attention: AttentionOut
    opportunities: list[OpportunityOut]
    search: SearchOut | None
    analytics: AnalyticsOut
    recent_changes: list[ChangeOut]
    freshness: FreshnessOut
    # What the screen should say when it has nothing useful yet, rather than
    # rendering a page of zeroes.
    setup_hint: str | None


@router.get("/{website_id}/dashboard", response_model=DashboardOut)
async def dashboard(scope: WebsiteScopeDep, conn: ConnectionDep) -> DashboardOut:
    website_id = scope.website.id
    repo = DashboardRepository(conn)
    performance = PerformanceRepository(conn)
    analytics_repo = AnalyticsRepository(conn)

    end = date.today() - timedelta(days=GSC_LAG_DAYS)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=WINDOW_DAYS - 1)

    score = await _score(repo, website_id, end)
    attention_rows = await repo.attention(website_id)
    rising = await repo.rising_keywords(
        website_id, start, end, prior_start, prior_end
    )

    totals_row = await performance.totals(website_id, start, end)
    totals = Totals.of(totals_row)
    search = None
    if totals.impressions:
        before = Totals.of(await performance.totals(website_id, prior_start, prior_end))
        anonymised = await performance.anonymised_share(website_id, start, end)
        search = SearchOut(
            clicks=totals.clicks,
            impressions=totals.impressions,
            ctr=totals.ctr,
            position=totals.position,
            change={
                "clicks": pct_change(totals.clicks, before.clicks),
                "impressions": pct_change(totals.impressions, before.impressions),
                # Inverted once here: position improving means the number
                # going down, and every chart downstream would otherwise have
                # to remember that.
                "position": pct_change(before.position, totals.position),
            } if before.impressions else None,
            # Zero unless query data actually exists for the window: with
            # none synced, the whole total would otherwise be reported as
            # withheld by Google, which is a different and untrue claim.
            anonymised_clicks=(
                int((anonymised or {}).get("anonymised_clicks") or 0)
                if (anonymised or {}).get("has_query_data") else 0
            ),
            note=(
                "Google withholds low-volume searches, so the query list does "
                "not add up to these totals."
            ),
        )

    ga_totals = await analytics_repo.totals(website_id, start, end) or {}
    analytics = AnalyticsOut(
        connected=bool(ga_totals.get("sessions")),
        sessions=int(ga_totals.get("sessions") or 0),
        active_users=int(ga_totals.get("active_users") or 0),
        engagement_rate=(
            float(ga_totals["engagement_rate"])
            if ga_totals.get("engagement_rate") is not None else None
        ),
        outcomes_configured=await analytics_repo.has_goals(website_id),
    )

    crawl = await repo.last_crawl(website_id)
    sync = await repo.last_sync(website_id)

    return DashboardOut(
        website={
            "id": str(scope.website.id),
            "domain": scope.website.domain,
            "name": scope.website.name,
            "status": scope.website.status.value,
            "ownership_verified": scope.website.ownership_verified,
        },
        score=score,
        attention=_attention(attention_rows, rising),
        opportunities=_opportunities(attention_rows),
        search=search,
        analytics=analytics,
        recent_changes=[
            ChangeOut(
                observed_at=r["observed_at"],
                kind=("fixed" if not r["present"]
                      else "returned" if r["status"] == "regressed" else "verified"),
                title=r["title"],
                url=r["url"],
            )
            for r in await repo.recent_changes(website_id)
        ],
        freshness=FreshnessOut(
            last_crawl_at=(crawl or {}).get("finished_at"),
            pages_crawled=(crawl or {}).get("pages_fetched"),
            search_data_through=(sync or {}).get("range_end"),
            stale=_is_stale(crawl),
        ),
        setup_hint=_setup_hint(scope, search, crawl),
    )


@router.get("/{website_id}/score-history")
async def score_history(scope: WebsiteScopeDep, conn: ConnectionDep) -> dict:
    rows = await DashboardRepository(conn).score_history(scope.website.id)
    return {
        # Stated explicitly: the chart is only comparable within one version,
        # and a reader deserves to know which one they are looking at.
        "scoring_version": rows[0]["scoring_version"] if rows and
        "scoring_version" in rows[0] else None,
        "points": [
            {
                "as_of": r["as_of"],
                "total": float(r["total"]),
                "technical_health": _f(r["technical_health"]),
                "search_performance": _f(r["search_performance"]),
                "content_health": _f(r["content_health"]),
                "analytics_coverage": _f(r["analytics_coverage"]),
            }
            for r in rows
        ],
    }


async def _score(repo: DashboardRepository, website_id, end: date) -> ScoreOut | None:
    latest = await repo.latest_score(website_id)
    if latest is None:
        return None

    previous = await repo.score_before(website_id, latest["as_of"] - timedelta(days=28))
    change = pct_change(
        latest["total"], previous["total"] if previous else None
    )

    return ScoreOut(
        total=float(latest["total"]),
        as_of=latest["as_of"],
        components={
            key: _f(latest[key])
            for key in ("technical_health", "search_performance",
                        "content_health", "analytics_coverage", "ai_visibility")
        },
        change_pct=change,
        compared_to=previous["as_of"] if previous else None,
        is_first_measurement=previous is None,
    )


def _attention(rows: list[dict], rising: int) -> AttentionOut:
    critical = sum(r["n"] for r in rows if r["severity"] == "critical")
    content = sum(r["n"] for r in rows if r["category"] in ("content", "ai_search"))
    losing = sum(r["n"] for r in rows if r["type_key"] == "declining_page")
    return AttentionOut(
        critical=critical,
        content_opportunities=content,
        losing_visibility=losing,
        gaining_visibility=rising,
    )


def _opportunities(rows: list[dict]) -> list[OpportunityOut]:
    ordered = sorted(
        rows,
        key=lambda r: (-(float(r["impact"] or 0)),
                       SEVERITY_ORDER.index(r["severity"])
                       if r["severity"] in SEVERITY_ORDER else 9),
    )
    return [
        OpportunityOut(
            type_key=r["type_key"],
            headline=phrase_for(r["type_key"], int(r["n"]), r["title"]),
            count=int(r["n"]),
            estimated_clicks=int(float(r["impact"] or 0)),
            severity=r["severity"],
            category=r["category"],
        )
        for r in ordered[:MAX_OPPORTUNITIES]
    ]


def _setup_hint(scope, search, crawl) -> str | None:
    if not scope.website.ownership_verified:
        return "Connect Google Search Console to see how people find you."
    if search is None:
        return "We're waiting for your first search data from Google."
    if crawl is None:
        return "We haven't scanned your website yet."
    return None


def _is_stale(crawl) -> bool:
    finished = (crawl or {}).get("finished_at")
    if finished is None:
        return True
    return (datetime.now(finished.tzinfo) - finished) > timedelta(days=10)


def _f(value) -> float | None:
    return float(value) if value is not None else None
