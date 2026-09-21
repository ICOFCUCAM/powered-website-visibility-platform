"""Search Console rules — the opportunities, which is where the money is.

Every rule here needs GSC data, which is why the wizard connects Google before
it crawls: a site with no search data gets a technical audit, and a site with
it gets an opportunity list.
"""

from __future__ import annotations

from collections.abc import Iterable

from api.analysis.fingerprint import keyword_scope, page_scope
from api.analysis.rules.base import AnalysisContext, Finding, rule

#: Below this, a CTR is a rounding artefact rather than a signal.
MIN_IMPRESSIONS_FOR_CTR = 200

STRIKING_DISTANCE = (11.0, 20.0)
MIN_IMPRESSIONS_FOR_KEYWORD = 100

#: Where a striking-distance keyword could plausibly land. Not a promise —
#: used only to size the opportunity.
STRIKING_TARGET_POSITION = 8.0


@rule("ctr_below_position_baseline")
def ctr_below_position_baseline(ctx: AnalysisContext) -> Iterable[Finding]:
    """The rule the whole CTR baseline exists for.

    Compares against what this position normally earns ON THIS SITE, so a
    perfectly healthy page at position 18 is never flagged for having the same
    CTR that would be dreadful at position 3.
    """
    curve = ctx.ctr_curve
    if curve is None:
        return

    for performance in ctx.page_performance.values():
        if performance.impressions < MIN_IMPRESSIONS_FOR_CTR:
            continue
        if not curve.underperforms(performance.ctr, performance.position):
            continue

        expected = curve.expected(performance.position)
        recoverable = curve.shortfall(performance.ctr, performance.position)
        yield Finding(
            "ctr_below_position_baseline", "page", page_scope(performance.url_hash),
            {
                "url": performance.url,
                "impressions": performance.impressions,
                "clicks": performance.clicks,
                "ctr": round(performance.ctr, 4),
                "position": round(performance.position, 1),
                "expected_ctr": round(expected, 4),
                "clicks_at_expected": round(performance.impressions * expected),
                # The customer deserves to know whether "expected" came from
                # their own site or from a published average.
                "baseline_source": (
                    "your site" if curve.is_measured(performance.position)
                    else "typical results"
                ),
            },
            severity="high" if performance.impressions > 2000 else "medium",
            impact=recoverable * performance.impressions,
        )


@rule("striking_distance_keyword")
def striking_distance_keyword(ctx: AnalysisContext) -> Iterable[Finding]:
    """Searches sitting just below the first page.

    The cheapest wins on any site: the page already ranks, it is just on the
    wrong side of a line almost nobody crosses.
    """
    curve = ctx.ctr_curve
    low, high = STRIKING_DISTANCE

    for query in ctx.queries:
        if query.impressions < MIN_IMPRESSIONS_FOR_KEYWORD:
            continue
        if not (low <= query.position <= high):
            continue

        gain = 0.0
        if curve is not None:
            target = curve.expected(STRIKING_TARGET_POSITION)
            gain = max(0.0, target - query.ctr) * query.impressions

        yield Finding(
            "striking_distance_keyword", "keyword", keyword_scope(query.phrase),
            {
                "query": query.phrase,
                "position": round(query.position, 1),
                "impressions": query.impressions,
                "clicks": query.clicks,
                "target_position": STRIKING_TARGET_POSITION,
            },
            impact=gain,
        )


#: A page has to have been worth something to be worth losing.
MIN_PRIOR_CLICKS_FOR_DECLINE = 20
DECLINE_RATIO = 0.7


@rule("declining_page")
def declining_page(ctx: AnalysisContext) -> Iterable[Finding]:
    """Pages getting noticeably fewer clicks than they were.

    Compared against the equivalent window immediately before, so week-on-week
    seasonality does not read as decline. Silent when there is no prior window
    — a new account has nothing to compare against, and inventing a baseline
    would greet them with a list of phantom problems.
    """
    if not ctx.prior_page_performance:
        return

    for url_hash, now in ctx.page_performance.items():
        before = ctx.prior_page_performance.get(url_hash)
        if before is None or before.clicks < MIN_PRIOR_CLICKS_FOR_DECLINE:
            continue
        if now.clicks >= before.clicks * DECLINE_RATIO:
            continue

        lost = before.clicks - now.clicks
        yield Finding(
            "declining_page", "page", page_scope(url_hash),
            {
                "url": now.url,
                "clicks": now.clicks,
                "clicks_before": before.clicks,
                "change_pct": round(
                    (now.clicks - before.clicks) / before.clicks * 100, 1
                ),
                "position": round(now.position, 1),
                "position_before": round(before.position, 1),
            },
            severity="high" if lost > 50 else "medium",
            impact=float(lost),
        )


@rule("cannibalisation")
def cannibalisation(ctx: AnalysisContext) -> Iterable[Finding]:
    """Two pages taking turns for one search.

    Detected from the query x page dataset when it is available; without it
    the rule stays silent rather than guessing from correlation.
    """
    return ()
