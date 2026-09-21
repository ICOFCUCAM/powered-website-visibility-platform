"""Render escalation.

A headless browser costs roughly 50x the time and memory of an HTTP fetch, so
rendering is a PER-URL decision, not a per-project one. The default is a plain
fetch; a page earns a render by showing that its content is not in the HTML.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from api.crawler.extract import PageFacts

#: Below this, whatever the page is, it is not the article the user thinks it
#: is. Chosen to sit well under a real page's body text and well above an
#: empty SPA shell.
MIN_BODY_TEXT = 200
SUBSTANTIAL_SCRIPT_BYTES = 50_000

#: Rendering every page of a 500-page site would take hours. A budget keeps
#: the decision honest: we render the pages most likely to need it.
DEFAULT_RENDER_BUDGET = 50


@dataclass(frozen=True, slots=True)
class RenderDecision:
    render: bool
    reason: str | None = None


def should_render(
    facts: PageFacts,
    *,
    budget_remaining: int,
    force: bool = False,
) -> RenderDecision:
    if force:
        return RenderDecision(True, "site_configured_always_render")
    if budget_remaining <= 0:
        # Not "no": "not this time". The distinction matters in the crawl
        # summary, because a truncated render budget explains missing findings.
        return RenderDecision(False, "render_budget_exhausted")

    thin = facts.body_text_length < MIN_BODY_TEXT
    scripted = facts.script_bytes > SUBSTANTIAL_SCRIPT_BYTES

    if thin and scripted:
        return RenderDecision(True, "content_appears_javascript_dependent")
    if facts.has_spa_root and not facts.h1:
        return RenderDecision(True, "spa_shell_without_heading")
    return RenderDecision(False, None)


class Renderer(Protocol):
    """Implemented by Playwright in production.

    Kept behind a Protocol so the crawler can be tested without a browser, and
    so render workers can run in their own pool with hard memory limits —
    Playwright OOMs are the classic way a shared pool takes the analysis
    pipeline down with it.
    """

    async def render(self, url: str) -> str | None: ...


class NoRenderer:
    """The default when no browser is configured. Honest about it: the crawl
    records that rendering was unavailable rather than pretending a shell was
    the whole page."""

    async def render(self, url: str) -> str | None:
        return None
