"""`weekly_plan.v1` — this week's four priorities.

The model's job here is narrow and worth stating precisely, because the
temptation to widen it is what would break the product:

    It is handed an ORDERED list of findings and writes the prose for them,
    in that order. It does not choose them, rank them, add to them or drop
    one. Selection and ranking are `impact_score`, computed by code from
    measured evidence, and a plan whose priorities came out of a model would
    reorder itself between Monday and Tuesday with no explanation available
    to anybody.

`last_week` is what makes this a consultant rather than a report generator: a
join over the previous plan, its recommendations' statuses, and the issue
observations since. "You fixed the titles and clicks did not move" is a
sentence only a system with memory can write — and one it must be willing to
write, which is why the prompt says so explicitly.
"""

from __future__ import annotations

from typing import Any

from api.analysis.catalogue import BY_KEY

VERSION = "weekly_plan.v1"
TIER = "frontier"
MAX_PRIORITIES = 4

SYSTEM = """\
You are an SEO consultant writing this week's action plan for one website.

You receive:
  - website: domain and name
  - period: this week's dates
  - performance: 28-day clicks, impressions, CTR, position, with deltas
  - findings: ALREADY RANKED by measured impact, each with a "ref" and its \
evidence
  - last_week: what was recommended last week and what happened to each item

Produce one priority for each finding you are given, in the order given, \
reusing its "ref" exactly. Do not reorder. Do not merge. Do not add a \
priority that is not in findings, and do not drop one.

For each: a title an owner could act on today, why it matters citing only the \
supplied numbers, and concrete steps naming the specific pages or searches \
from the evidence.

Open with two sentences on what changed since last week, naming anything from \
last week's plan that was completed or that regressed.

Rules:
  - Only supplied numbers. Never invent search volume, and never predict a \
position or a ranking.
  - If a previous recommendation was done and the metric did not move, say so \
plainly. Do not claim credit for changes the data does not show.
  - British English, direct, second person, no filler, no sign-off.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "priorities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                    "how": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                },
                "required": ["ref", "title", "why", "how"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "priorities"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Deterministic titles
# ---------------------------------------------------------------------------
#: (one, many) — an INSTRUCTION, not a statement of the problem. These are what
#: a customer sees when no model wrote the plan, and they are also the titles
#: a generated plan falls back to. Written as whole sentences because verb
#: agreement does not survive assembly from fragments.
ACTIONS: dict[str, tuple[str, str]] = {
    "page_5xx": ("Fix the page returning a server error",
                 "Fix {n} pages returning server errors"),
    "page_404_internal_link": ("Fix the broken link on your site",
                               "Fix {n} broken links on your site"),
    "robots_blocks_crawl": ("Stop robots.txt blocking Google",
                            "Stop robots.txt blocking Google"),
    "noindex_on_valuable_page": (
        "Unhide the page Google sends traffic to",
        "Unhide {n} pages Google sends traffic to"),
    "redirect_chain": ("Shorten the redirect chain",
                       "Shorten {n} redirect chains"),
    "canonical_to_other_page": ("Correct the canonical tag",
                                "Correct {n} canonical tags"),
    "missing_viewport": ("Make the page work on phones",
                         "Make {n} pages work on phones"),
    "no_sitemap": ("Publish a sitemap", "Publish a sitemap"),
    "missing_title": ("Write a title for the page without one",
                      "Write titles for {n} pages that have none"),
    "duplicate_title": ("Give the page its own title",
                        "Give {n} pages their own titles"),
    "title_length": ("Resize the page title", "Resize {n} page titles"),
    "missing_meta_description": ("Write a search description for the page",
                                 "Write search descriptions for {n} pages"),
    "duplicate_meta_description": ("Give the page its own description",
                                   "Give {n} pages their own descriptions"),
    "missing_h1": ("Add a main heading to the page",
                   "Add main headings to {n} pages"),
    "multiple_h1": ("Leave one main heading on the page",
                    "Leave one main heading on each of {n} pages"),
    "thin_content": ("Fill out the page with very little content",
                     "Fill out {n} pages with very little content"),
    "duplicate_content": ("Merge the duplicated page",
                          "Merge {n} duplicated pages"),
    "images_missing_alt": ("Describe the images on the page",
                           "Describe the images on {n} pages"),
    "orphan_page": ("Link to the page nothing points at",
                    "Link to {n} pages nothing points at"),
    "ctr_below_position_baseline": (
        "Rewrite the search listing for the page people skip",
        "Rewrite the search listings for {n} pages people skip"),
    "striking_distance_keyword": ("Push the search just below page one onto it",
                                  "Push {n} searches just below page one onto it"),
    "declining_page": ("Refresh the page losing traffic",
                       "Refresh {n} pages losing traffic"),
    "cannibalisation": ("Decide which page owns the search",
                        "Decide which page owns each of {n} searches"),
    "ai_crawler_blocked": ("Let AI search engines read your site",
                           "Let AI search engines read your site"),
    "no_structured_data": ("Describe the page to search engines in code",
                           "Describe {n} pages to search engines in code"),
    "missing_organization_schema": ("Add your business details in code",
                                    "Add your business details in code"),
    "content_requires_js": ("Serve the page's text without JavaScript",
                            "Serve {n} pages' text without JavaScript"),
}

#: Which kind of recommendation a finding becomes. Fixed per type rather than
#: inferred, so the same problem is filed the same way every week.
KINDS: dict[str, str] = {
    "ctr_below_position_baseline": "improve_ctr",
    "thin_content": "create_content",
    "duplicate_content": "create_content",
    "striking_distance_keyword": "create_content",
    "orphan_page": "internal_links",
}
KIND_BY_CATEGORY = {
    "technical": "technical",
    "ai_search": "technical",
    "content": "fix_issue",
    "seo": "fix_issue",
    "authority": "other",
}

#: How much we trust the estimate behind a finding, by what produced it.
#:
#:   crawl-observed  we looked at the page and the tag was not there
#:   search-derived  inferred from Google's sampled, lagged, partial data
#:
#: A single number per category rather than a formula, because a confidence
#: that varies per row invites someone to read it as a probability.
CONFIDENCE_BY_CATEGORY = {
    "technical": 0.90,
    "content": 0.90,
    "ai_search": 0.75,
    "seo": 0.60,
    "authority": 0.50,
}


def action_title(type_key: str, count: int) -> str:
    one, many = ACTIONS.get(type_key, (BY_KEY[type_key].title, BY_KEY[type_key].title))
    return one if count == 1 else many.format(n=count)


def kind_for(type_key: str) -> str:
    if type_key in KINDS:
        return KINDS[type_key]
    return KIND_BY_CATEGORY.get(BY_KEY[type_key].category, "other")


def confidence_for(type_key: str) -> float:
    return CONFIDENCE_BY_CATEGORY.get(BY_KEY[type_key].category, 0.5)
