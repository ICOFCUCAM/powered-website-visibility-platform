"""The expected-CTR curve.

This is the single rule most able to ruin the product: flag low CTR without a
position baseline and the customer drowns in non-issues, then stops reading
the list the whole product is built around.
"""

from __future__ import annotations

import pytest

from api.analysis.ctr_baseline import (
    GLOBAL_CURVE,
    MIN_IMPRESSIONS_PER_BUCKET,
    CtrCurve,
    curve_from_buckets,
)


def bucket(position: int, clicks: int, impressions: int) -> dict:
    return {"position_bucket": position, "clicks": clicks, "impressions": impressions}


def test_the_same_ctr_is_a_problem_at_position_three_and_normal_at_eighteen():
    """4,821 impressions and 37 clicks is 0.77%."""
    curve = CtrCurve(by_position=dict(GLOBAL_CURVE))
    measured = 37 / 4821

    assert curve.underperforms(measured, position=3.0)
    assert not curve.underperforms(measured, position=18.0)


def test_a_site_with_enough_data_sets_its_own_baseline():
    """A church ranking first for its own name converts far above any published
    average. Judging it by the global curve would call a healthy page broken."""
    curve = curve_from_buckets([bucket(1, 700, 1000)])

    assert curve.expected(1.0) == pytest.approx(0.70)
    assert curve.is_measured(1.0)
    # A 40% CTR at position one is below THIS site's normal, and the finding
    # is real even though it towers over the global 27%.
    assert curve.underperforms(0.40, position=1.0)


def test_a_thin_bucket_falls_back_rather_than_inventing_a_baseline():
    curve = curve_from_buckets([bucket(4, 9, MIN_IMPRESSIONS_PER_BUCKET - 1)])
    assert curve.expected(4.0) == GLOBAL_CURVE[4]
    assert not curve.is_measured(4.0)


def test_provenance_is_per_bucket_not_per_site():
    """A site can have plenty of data at position 1 and none at position 12.
    Saying "your site normally achieves" about a borrowed number would be a
    small lie repeated on every finding."""
    curve = curve_from_buckets([bucket(1, 700, 1000), bucket(12, 1, 10)])
    assert curve.is_measured(1.0)
    assert not curve.is_measured(12.0)


def test_positions_are_bucketed_by_rounding():
    curve = curve_from_buckets([bucket(3, 100, 1000)])
    assert curve.expected(2.6) == curve.expected(3.4) == pytest.approx(0.10)
    assert curve.expected(3.5) != pytest.approx(0.10)


def test_anything_past_the_curve_uses_the_tail():
    curve = CtrCurve(by_position=dict(GLOBAL_CURVE))
    assert curve.expected(45.0) == GLOBAL_CURVE[20]
    assert curve.expected(0.4) == GLOBAL_CURVE[1]


def test_the_shortfall_is_the_ranking_currency():
    """Impact is estimated additional clicks, which is comparable across every
    rule type and defensible to a customer."""
    curve = CtrCurve(by_position=dict(GLOBAL_CURVE))
    impressions, clicks = 4821, 37
    measured = clicks / impressions

    shortfall = curve.shortfall(measured, position=3.0)
    recoverable = shortfall * impressions

    assert shortfall == pytest.approx(0.10 - measured, abs=1e-6)
    assert 440 < recoverable < 450


def test_a_page_performing_above_expectation_has_no_shortfall():
    curve = CtrCurve(by_position=dict(GLOBAL_CURVE))
    assert curve.shortfall(0.35, position=3.0) == 0.0
    assert not curve.underperforms(0.35, position=3.0)


def test_the_threshold_is_generous_on_purpose():
    """The cost of a false positive is the customer distrusting every other
    finding, so "a bit below" is not a finding."""
    curve = CtrCurve(by_position=dict(GLOBAL_CURVE))
    expected = GLOBAL_CURVE[5]
    assert not curve.underperforms(expected * 0.8, position=5.0)
    assert curve.underperforms(expected * 0.4, position=5.0)
