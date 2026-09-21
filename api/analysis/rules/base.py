"""The rule framework.

A rule is a pure function from context to findings. It never touches the
database, never calls a model, and never decides priority — it states what it
observed, with the evidence, and the framework does the rest.

That separation is what makes the loop verifiable: the same crawl run through
the same rule version yields the same findings, so "this is fixed" is a fact
rather than an opinion.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from api.analysis.catalogue import BY_KEY
from api.analysis.ctr_baseline import CtrCurve
from api.analysis.fingerprint import fingerprint


@dataclass(frozen=True, slots=True)
class PageRow:
    """One page as the latest crawl saw it."""

    page_id: Any
    url: str
    url_hash: bytes
    status_code: int | None
    title: str | None
    meta_description: str | None
    h1: list[str]
    word_count: int
    canonical_url: str | None
    canonical_is_self: bool | None
    robots_meta: list[str]
    viewport_present: bool | None
    schema_types: list[str]
    images_total: int
    images_missing_alt: int
    internal_outlinks: int
    text_hash: bytes | None
    redirect_chain: list[dict] | None
    depth: int | None
    render_mode: str
    body_text_length: int = 0

    @property
    def is_indexable(self) -> bool:
        return "noindex" not in (self.robots_meta or [])

    @property
    def is_ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


@dataclass(frozen=True, slots=True)
class QueryRow:
    phrase: str
    clicks: int
    impressions: int
    position: float

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0


@dataclass(frozen=True, slots=True)
class PagePerformance:
    url_hash: bytes
    url: str
    clicks: int
    impressions: int
    position: float

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0


@dataclass
class AnalysisContext:
    """Everything the rules may look at. Assembled once per analysis run."""

    website_id: Any
    origin: str
    pages: list[PageRow] = field(default_factory=list)
    queries: list[QueryRow] = field(default_factory=list)
    page_performance: dict[bytes, PagePerformance] = field(default_factory=dict)
    #: The equivalent window immediately before, for change detection. Empty
    #: on a site with too little history, and the rules that need it stay
    #: silent rather than comparing against nothing.
    prior_page_performance: dict[bytes, PagePerformance] = field(default_factory=dict)
    linked_url_hashes: set[bytes] = field(default_factory=set)
    broken_link_targets: dict[bytes, str] = field(default_factory=dict)
    ctr_curve: CtrCurve | None = None
    ai_crawlers_blocked: list[str] = field(default_factory=list)
    sitemap_found: bool = True
    robots_blocks_crawl: bool = False
    window_start: date | None = None
    window_end: date | None = None

    @property
    def has_search_data(self) -> bool:
        return bool(self.queries or self.page_performance)

    def performance_for(self, page: PageRow) -> PagePerformance | None:
        return self.page_performance.get(page.url_hash)


@dataclass(frozen=True, slots=True)
class Finding:
    type_key: str
    scope_type: str
    scope_ref: str | None
    evidence: dict[str, Any]
    severity: str | None = None
    #: Estimated additional monthly clicks. The one currency every rule
    #: reports in, so findings of different kinds can be ranked against each
    #: other without anyone's opinion entering.
    impact: float = 0.0
    page_id: Any = None

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.type_key, self.scope_type, self.scope_ref)

    @property
    def resolved_severity(self) -> str:
        return self.severity or BY_KEY[self.type_key].severity


Rule = Callable[[AnalysisContext], Iterable[Finding]]

_REGISTRY: list[tuple[str, Rule]] = []


def rule(key: str) -> Callable[[Rule], Rule]:
    """Registers a rule and ties it to its catalogue entry.

    Registering an unknown key fails at import, not at run time: a rule whose
    catalogue row is missing would emit issues with no title or severity.
    """

    def decorate(fn: Rule) -> Rule:
        if key not in BY_KEY:
            raise KeyError(f"rule {key!r} has no entry in the issue catalogue")
        _REGISTRY.append((key, fn))
        return fn

    return decorate


def all_rules() -> list[tuple[str, Rule]]:
    return list(_REGISTRY)
