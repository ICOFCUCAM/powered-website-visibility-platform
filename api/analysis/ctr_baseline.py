"""Expected click-through rate, by position.

THE RULE THIS MODULE EXISTS TO MAKE POSSIBLE:

    4,821 impressions and 37 clicks is 0.77%. At position 3 that is dreadful.
    At position 18 it is completely normal.

A rule that flags low CTR without a position baseline generates hundreds of
non-issues, and a customer who learns to ignore the recommendation list has
left the loop the product is built around. So the comparison is always against
what CTR SHOULD be at that position.

The curve is derived per site, because branded and local niches differ wildly
from any global average: a church's own name converts at 70% in position one,
a comparison query at 12%. A global fallback covers positions the site has too
little data for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all

#: Below this a bucket is noise, not a baseline.
MIN_IMPRESSIONS_PER_BUCKET = 50

#: How far below expected counts as a finding. Deliberately generous: the cost
#: of a false positive here is the customer distrusting every other finding.
UNDERPERFORMANCE_RATIO = 0.6

MAX_POSITION = 20

#: Fallback, used per-bucket where the site has too little of its own data.
GLOBAL_CURVE: dict[int, float] = {
    1: 0.270, 2: 0.150, 3: 0.100, 4: 0.070, 5: 0.050,
    6: 0.040, 7: 0.030, 8: 0.025, 9: 0.020, 10: 0.018,
    11: 0.012, 12: 0.011, 13: 0.010, 14: 0.009, 15: 0.008,
    16: 0.008, 17: 0.007, 18: 0.007, 19: 0.006, 20: 0.006,
}


@dataclass(frozen=True, slots=True)
class CtrCurve:
    """Expected CTR per integer position, with provenance per bucket."""

    by_position: dict[int, float]
    derived_positions: frozenset[int] = field(default_factory=frozenset)

    def expected(self, position: float) -> float:
        bucket = max(1, min(MAX_POSITION, int(round(position))))
        return self.by_position.get(bucket, GLOBAL_CURVE[MAX_POSITION])

    def is_measured(self, position: float) -> bool:
        """True when this site's own data set the baseline.

        Surfaced with the finding: "below what your site normally achieves at
        this position" is a stronger claim than "below a published average",
        and the customer deserves to know which one they are being told.
        """
        return max(1, min(MAX_POSITION, int(round(position)))) in self.derived_positions

    def underperforms(self, ctr: float, position: float) -> bool:
        return ctr < self.expected(position) * UNDERPERFORMANCE_RATIO

    def shortfall(self, ctr: float, position: float) -> float:
        """Estimated additional clicks per impression if it performed normally.

        This is the ranking currency: multiplied by impressions it gives
        estimated additional monthly clicks, which is comparable across every
        rule type and defensible to a customer.
        """
        return max(0.0, self.expected(position) - ctr)


def curve_from_buckets(buckets: list[dict]) -> CtrCurve:
    by_position = dict(GLOBAL_CURVE)
    derived: set[int] = set()

    for row in buckets:
        position = int(row["position_bucket"])
        impressions = int(row["impressions"] or 0)
        clicks = int(row["clicks"] or 0)
        if position < 1 or position > MAX_POSITION:
            continue
        if impressions < MIN_IMPRESSIONS_PER_BUCKET:
            continue
        by_position[position] = clicks / impressions
        derived.add(position)

    return CtrCurve(by_position=by_position, derived_positions=frozenset(derived))


async def build_curve(
    conn: AsyncConnection, website_id: UUID, start: date, end: date
) -> CtrCurve:
    """Bucket the site's own query data by rounded position.

    Weighted naturally: summing clicks and impressions across a bucket gives
    the bucket's true CTR, rather than averaging per-row rates and letting a
    one-impression row count as much as a thousand-impression one.
    """
    rows = await fetch_all(
        conn,
        """
        select greatest(1, least(%s, round(position)))::int as position_bucket,
               sum(clicks)      as clicks,
               sum(impressions) as impressions
          from gsc_query_daily
         where website_id = %s and date between %s and %s
           and position <= %s
         group by 1
        """,
        (MAX_POSITION, website_id, start, end, MAX_POSITION),
    )
    return curve_from_buckets(rows)
