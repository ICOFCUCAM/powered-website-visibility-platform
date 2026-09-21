"""The Visibility Health Score (V1 spec s17).

This is OUR score, computed by this platform. It is never presented as a
Google score, because it is not one.

Rules for changing it, from the frozen decisions:

  1. Bump `SCORING_VERSION`.
  2. Recompute every historical snapshot under the new version.
  3. Render the chart from a single version throughout.

A score that moves because weights changed silently is a lie told in a chart.

Components come from the `score_components` catalogue rather than from
constants here, so adding "Maps visibility" later is a config row and a
recompute — not an edit to this file and a silent shift in everyone's number.
A component with no data source is ABSENT from the weighting, never zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCORING_VERSION = "1.0.0"

SEVERITY_MULTIPLIER = {
    "critical": 1.0,
    "high": 0.7,
    "medium": 0.4,
    "low": 0.15,
    "info": 0.0,
}


@dataclass(frozen=True, slots=True)
class Component:
    key: str
    label: str
    weight: float
    data_source: str
    is_modelled: bool


@dataclass(frozen=True, slots=True)
class ComponentScore:
    key: str
    label: str
    score: float
    weight: float
    is_modelled: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Scorecard:
    total: float
    components: list[ComponentScore]
    scoring_version: str = SCORING_VERSION

    def by_key(self) -> dict[str, ComponentScore]:
        return {c.key: c for c in self.components}

    def as_components_json(self) -> dict[str, Any]:
        """Every input that produced the number, so any point on the chart can
        be explained without re-deriving it."""
        return {
            c.key: {
                "label": c.label,
                "score": c.score,
                "weight": c.weight,
                "is_modelled": c.is_modelled,
                **c.detail,
            }
            for c in self.components
        }


#: Which issue category feeds which scorecard component.
CATEGORY_TO_COMPONENT = {
    "technical": "technical_health",
    "content": "content_health",
    "seo": "search_performance",
    "ai_search": "ai_visibility",
}


def penalty_for(
    issues: list[dict[str, Any]], evaluated_pages: int
) -> tuple[float, dict[str, Any]]:
    """Weighted penalty, normalised by how many pages were looked at.

    Normalising matters: without it a 1,000-page site is punished for having
    more of everything, and a five-page site scores well by having nowhere for
    problems to hide.
    """
    if not issues:
        return 0.0, {"issues": 0, "pages_evaluated": evaluated_pages}

    pages = max(1, evaluated_pages)
    weighted = 0.0
    total_weight = 0.0
    counts: dict[str, int] = {}

    for issue in issues:
        weight = float(issue["weight"])
        multiplier = SEVERITY_MULTIPLIER.get(issue["severity"], 0.4)
        affected = int(issue["affected"])
        scope = issue.get("scope_type", "page")

        # A website-wide issue affects the whole site by definition; a page
        # issue affects the share of pages it was found on.
        share = 1.0 if scope == "website" else min(1.0, affected / pages)

        weighted += weight * multiplier * share
        total_weight += weight
        counts[issue["type_key"]] = affected

    penalty = 100 * weighted / total_weight if total_weight else 0.0
    return min(100.0, penalty), {
        "issues": len(issues),
        "pages_evaluated": evaluated_pages,
        "by_type": counts,
    }


def score_components(
    components: list[Component],
    issues_by_category: dict[str, list[dict[str, Any]]],
    evaluated_pages: int,
    measured: dict[str, float | None] | None = None,
) -> Scorecard:
    """Assemble the scorecard.

    `measured` supplies components that are measured rather than
    penalty-based — search_performance is computed from actual Search Console
    movement, not from a count of problems. A measured component with a None
    value is absent: its weight is redistributed rather than counted as zero.
    """
    measured = measured or {}
    scored: list[ComponentScore] = []

    for component in components:
        if component.key in measured:
            value = measured[component.key]
            if value is None:
                # No data source yet. Absent, not zero — showing 0 for
                # "we don't know" is a fabricated measurement.
                continue
            scored.append(
                ComponentScore(
                    component.key, component.label, round(value, 2),
                    component.weight, component.is_modelled,
                    {"source": component.data_source, "basis": "measured"},
                )
            )
            continue

        issues = issues_by_category.get(component.key, [])
        penalty, detail = penalty_for(issues, evaluated_pages)
        scored.append(
            ComponentScore(
                component.key, component.label, round(100 - penalty, 2),
                component.weight, component.is_modelled,
                {"source": component.data_source, "basis": "penalty", **detail},
            )
        )

    live_weight = sum(c.weight for c in scored)
    if live_weight <= 0:
        return Scorecard(total=0.0, components=scored)

    # Re-normalised over the components that actually have data, so an absent
    # component does not quietly drag the total down.
    total = sum(c.score * c.weight for c in scored) / live_weight
    return Scorecard(total=round(total, 2), components=scored)
