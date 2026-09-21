"""The Visibility Health Score."""

from __future__ import annotations

import pytest

from api.analysis.scoring import (
    SCORING_VERSION,
    Component,
    penalty_for,
    score_components,
)

COMPONENTS = [
    Component("technical_health", "Technical Health", 0.30, "crawl", False),
    Component("search_performance", "Search Performance", 0.35, "gsc", False),
    Component("content_health", "Content Health", 0.25, "crawl", False),
    Component("analytics_coverage", "Analytics Coverage", 0.10, "ga4", False),
]


def issue(type_key, severity, weight, affected, scope_type="page"):
    return {
        "type_key": type_key, "severity": severity,
        "weight": weight, "affected": affected, "scope_type": scope_type,
    }


def test_a_clean_site_scores_one_hundred():
    card = score_components(COMPONENTS, {}, evaluated_pages=50,
                            measured={"search_performance": 100.0,
                                      "analytics_coverage": 100.0})
    assert card.total == 100.0
    assert card.scoring_version == SCORING_VERSION


def test_the_penalty_is_normalised_by_pages_looked_at():
    """Without this a 1,000-page site is punished for having more of
    everything, and a five-page site scores well by having nowhere to hide."""
    small, _ = penalty_for([issue("missing_title", "critical", 5, 5)], 5)
    large, _ = penalty_for([issue("missing_title", "critical", 5, 5)], 500)

    assert small > large
    assert small == pytest.approx(100.0)   # every page affected
    assert large < 5


def test_a_site_wide_issue_is_not_diluted_by_page_count():
    """robots.txt blocking the site affects the whole site however many pages
    it has."""
    penalty, _ = penalty_for(
        [issue("robots_blocks_crawl", "critical", 5, 1, scope_type="website")], 1000
    )
    assert penalty == pytest.approx(100.0)


def test_severity_changes_how_much_a_problem_costs():
    critical, _ = penalty_for([issue("a", "critical", 3, 10)], 10)
    low, _ = penalty_for([issue("a", "low", 3, 10)], 10)
    assert critical > low
    assert low == pytest.approx(15.0)


def test_a_component_with_no_data_source_is_absent_not_zero():
    """Showing 0 for "we don't know" is a fabricated measurement, and it drags
    a perfectly healthy site's score down."""
    card = score_components(
        COMPONENTS, {}, evaluated_pages=10,
        measured={"search_performance": None, "analytics_coverage": None},
    )
    keys = {c.key for c in card.components}
    assert "search_performance" not in keys
    assert "analytics_coverage" not in keys
    # And the total is not dragged down by their absence.
    assert card.total == 100.0


def test_weights_are_renormalised_over_what_is_present():
    card = score_components(
        [COMPONENTS[0], COMPONENTS[1]],
        {"technical_health": [issue("a", "critical", 5, 10)]},
        evaluated_pages=10,
        measured={"search_performance": None},
    )
    # Only technical remains, so the total is exactly its score.
    assert len(card.components) == 1
    assert card.total == card.components[0].score


def test_a_measured_component_is_not_derived_from_issue_counts():
    """Search performance is computed from actual Search Console movement, not
    from a count of problems."""
    card = score_components(
        COMPONENTS,
        {"search_performance": [issue("x", "critical", 5, 100)]},
        evaluated_pages=100,
        measured={"search_performance": 68.0, "analytics_coverage": 91.0},
    )
    scores = card.by_key()
    assert scores["search_performance"].score == 68.0
    assert scores["search_performance"].detail["basis"] == "measured"
    assert scores["technical_health"].detail["basis"] == "penalty"


def test_every_input_is_recorded_so_a_chart_point_can_be_explained():
    card = score_components(
        COMPONENTS,
        {"technical_health": [issue("missing_viewport", "high", 3, 4)]},
        evaluated_pages=20,
        measured={"search_performance": 70.0, "analytics_coverage": 80.0},
    )
    blob = card.as_components_json()
    assert blob["technical_health"]["by_type"] == {"missing_viewport": 4}
    assert blob["technical_health"]["pages_evaluated"] == 20
    assert blob["technical_health"]["weight"] == 0.30


def test_the_penalty_cannot_push_a_component_below_zero():
    penalty, _ = penalty_for(
        [issue(f"i{i}", "critical", 5, 100) for i in range(20)], 100
    )
    assert penalty == 100.0
