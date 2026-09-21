"""`issue_explanation.v1` — explaining one kind of problem, once.

THE CACHE UNIT IS THE PROBLEM, NOT THE PAGE. A 500-page site with 200 missing
titles has one missing-title problem, explained once; the affected URLs are
rendered beside the explanation by code that cannot get them wrong.

That is what makes the cost claim in docs/09-mvp-sequence.md hold (a 500-page
crawl under $0.20 in explanations), and it is also the stronger privacy
position: the model is never shown a customer's URL, page title or search
query, so it cannot repeat one, mis-transcribe one, or invent one.

What it IS shown is an allowlist per issue type — FACETS below. A field not
named there cannot reach the prompt, which is a much easier property to audit
than "we remember to strip the URL".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from api.analysis.catalogue import BY_KEY
from api.analysis.rules.ai_visibility import JS_DEPENDENT_TEXT_LENGTH
from api.analysis.rules.content import (
    ALT_COVERAGE_THRESHOLD,
    THIN_CONTENT_WORDS,
    TITLE_MAX,
    TITLE_MIN,
)
from api.analysis.rules.search import (
    DECLINE_RATIO,
    MIN_IMPRESSIONS_FOR_CTR,
    MIN_IMPRESSIONS_FOR_KEYWORD,
    MIN_PRIOR_CLICKS_FOR_DECLINE,
    STRIKING_DISTANCE,
    STRIKING_TARGET_POSITION,
)
from api.analysis.rules.technical import MAX_REASONABLE_REDIRECTS

VERSION = "issue_explanation.v1"
TIER = "cheap"

SYSTEM = """\
You explain website SEO problems to a non-technical business owner.

You will receive one kind of detected problem as JSON, with the general facts \
that produced it. Write:
  - "what": one sentence naming the problem in plain language
  - "why": one or two sentences on why it costs them visibility
  - "how": concrete numbered steps to fix it on their website
  - "effort": one of low | medium | high

Rules:
  - Use ONLY numbers present in the input. Never estimate, extrapolate or \
invent a figure. If you want a number you were not given, omit the claim.
  - You have not been shown their pages, titles or search terms. Do not refer \
to a specific page, URL or phrase as though you had.
  - Never promise a ranking improvement. Describe the change, not the outcome.
  - No jargon without a plain-language gloss on first use.
  - British English. Second person. No preamble, no sign-off.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "what": {"type": "string"},
        "why": {"type": "string"},
        "how": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "effort": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["what", "why", "how", "effort"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Facets: the only evidence fields allowed into the prompt.
# ---------------------------------------------------------------------------
def _band(position: float | int | None) -> str:
    """Position as a band, not a number.

    A page at 11.4 and a page at 13.8 have the same problem and deserve the
    same explanation. Passing the exact figure would make every page its own
    cache entry and buy nothing.
    """
    if position is None:
        return "unknown"
    value = float(position)
    if value <= 3:
        return "top three"
    if value <= 10:
        return "first page"
    if value <= 20:
        return "second page"
    return "beyond the second page"


def _title_length(evidence: dict[str, Any]) -> dict[str, Any]:
    length = evidence.get("length")
    if not isinstance(length, (int, float)):
        return {}
    return {"direction": "too short" if length < 30 else "too long"}


def _redirect_chain(evidence: dict[str, Any]) -> dict[str, Any]:
    hops = evidence.get("hops")
    return {"hops": int(hops)} if isinstance(hops, (int, float)) else {}


def _position_band(evidence: dict[str, Any]) -> dict[str, Any]:
    return {"position_band": _band(evidence.get("position"))}


def _blocked_crawlers(evidence: dict[str, Any]) -> dict[str, Any]:
    crawlers = evidence.get("crawlers")
    # Names of public crawlers are not customer data.
    return {"crawlers": sorted(crawlers)} if isinstance(crawlers, list) else {}


#: The thresholds the rule actually applied, per type.
#:
#: These earn their place twice over. They make the advice concrete — "aim for
#: 30 to 60 characters" rather than "an appropriate length" — and they keep
#: that concreteness INSIDE rule 1, because the figure came from the rule that
#: produced the finding rather than from the model's memory of SEO folklore.
#:
#: Imported from the rule modules, never retyped. A threshold quoted in advice
#: that disagrees with the threshold that raised the issue is the worst of
#: both: confidently wrong, and wrong about our own behaviour.
THRESHOLDS: dict[str, dict[str, Any]] = {
    "missing_title": {"title_length": {"min": TITLE_MIN, "max": TITLE_MAX}},
    "title_length": {"title_length": {"min": TITLE_MIN, "max": TITLE_MAX}},
    "duplicate_title": {"title_length": {"min": TITLE_MIN, "max": TITLE_MAX}},
    "thin_content": {"minimum_words": THIN_CONTENT_WORDS},
    "images_missing_alt": {"target_alt_coverage": ALT_COVERAGE_THRESHOLD},
    "redirect_chain": {"maximum_hops": MAX_REASONABLE_REDIRECTS},
    "content_requires_js": {"minimum_text_length": JS_DEPENDENT_TEXT_LENGTH},
    # Not a rule threshold — a recommendation constant. It is here for the
    # same reason the thresholds are: the tag is a fact the product holds, so
    # quoting it is grounded rather than remembered, and both the template and
    # a generation may hand the customer something they can paste.
    "missing_viewport": {
        "recommended_tag": (
            '<meta name="viewport" '
            'content="width=device-width, initial-scale=1">'
        )
    },
    "ctr_below_position_baseline": {
        "minimum_impressions_considered": MIN_IMPRESSIONS_FOR_CTR
    },
    "striking_distance_keyword": {
        "positions_considered": list(STRIKING_DISTANCE),
        "target_position": STRIKING_TARGET_POSITION,
        "minimum_impressions": MIN_IMPRESSIONS_FOR_KEYWORD,
    },
    "declining_page": {
        "decline_ratio": DECLINE_RATIO,
        "minimum_prior_clicks": MIN_PRIOR_CLICKS_FOR_DECLINE,
    },
}


#: Per type, a function from raw evidence to the facts the model may see.
#: The default is an empty dict: the explanation is about the PROBLEM, and
#: anything a type does not explicitly need stays out.
FACETS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "title_length": _title_length,
    "redirect_chain": _redirect_chain,
    "ctr_below_position_baseline": _position_band,
    "striking_distance_keyword": _position_band,
    "declining_page": _position_band,
    "ai_crawler_blocked": _blocked_crawlers,
}


def payload_for(type_key: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """The complete prompt input, and therefore the complete cache key input."""
    issue_type = BY_KEY[type_key]
    facets = FACETS.get(type_key, lambda _: {})(evidence or {})
    return {
        "type_key": issue_type.key,
        "category": issue_type.category,
        "severity": issue_type.severity,
        "scope": issue_type.scope_type,
        "title": issue_type.title,
        "summary": issue_type.summary,
        "suggested_effort": issue_type.effort,
        "thresholds": THRESHOLDS.get(type_key, {}),
        "facts": facets,
    }


# ---------------------------------------------------------------------------
# The fallback
# ---------------------------------------------------------------------------
#: What a customer reads when no model ran — no provider configured, budget
#: spent, or a generation that failed validation. Written out in full rather
#: than assembled from fragments, because this is not a degraded mode anyone
#: should be embarrassed by: it is plain, correct advice.
TEMPLATE_FIXES: dict[str, tuple[str, ...]] = {
    "page_5xx": (
        "Open the page in a browser and confirm the error is still there.",
        "Check your server or hosting error log for the same address.",
        "Fix the underlying error, or remove the page and redirect it.",
    ),
    "page_404_internal_link": (
        "Find the link on the page that points at the missing address.",
        "Point it at the correct page, or remove the link.",
    ),
    "robots_blocks_crawl": (
        "Open yourdomain.com/robots.txt.",
        "Remove the Disallow: / line that blocks every crawler.",
        "Ask Google to recheck it in Search Console.",
    ),
    "noindex_on_valuable_page": (
        "Open the page's source and find the robots meta tag.",
        "Remove noindex, or change it to index.",
        "Request indexing for the page in Search Console.",
    ),
    "redirect_chain": (
        "Find the first address in the chain.",
        "Point it straight at the final address, so there is one hop.",
        "Update any internal links that still use the old address.",
    ),
    "canonical_to_other_page": (
        "Check whether this page really is a duplicate of the one it points at.",
        "If it is not, set the canonical tag to this page's own address.",
    ),
    "missing_viewport": (
        "Add <meta name=\"viewport\" content=\"width=device-width, "
        "initial-scale=1\"> to the page's head.",
        "Open the page on a phone and check nothing is cut off.",
    ),
    "no_sitemap": (
        "Generate a sitemap — most website platforms have this built in.",
        "Publish it at yourdomain.com/sitemap.xml.",
        "Submit it in Search Console.",
    ),
    "missing_title": (
        f"Write a title of {TITLE_MIN} to {TITLE_MAX} characters describing "
        "the page.",
        "Put the words someone would actually search for near the start.",
        "Make sure no other page on the site uses the same title.",
    ),
    "duplicate_title": (
        "Decide which page each title belongs to.",
        "Rewrite the others so each page's title describes only that page.",
    ),
    "title_length": (
        f"Rewrite the title to between {TITLE_MIN} and {TITLE_MAX} characters.",
        "Keep the most important words first, in case it is shortened.",
    ),
    "missing_meta_description": (
        # No length here: we do not measure description length, and quoting a
        # figure we did not check is exactly what the model is forbidden to do.
        "Write a short description summarising the page.",
        "Say what the visitor will get, not what the page contains.",
    ),
    "duplicate_meta_description": (
        "Rewrite the descriptions so each one describes only its own page.",
    ),
    "missing_h1": (
        "Add one main heading to the page, in an <h1> tag.",
        "Make it describe the page's subject in plain words.",
    ),
    "multiple_h1": (
        "Choose which heading is the page's main one and keep it as <h1>.",
        "Change the others to <h2>.",
    ),
    "thin_content": (
        f"Decide whether the page is worth keeping — it has under "
        f"{THIN_CONTENT_WORDS} words.",
        "If it is, answer the questions a visitor would arrive with.",
        "If it is not, merge it into a fuller page and redirect it.",
    ),
    "duplicate_content": (
        "Decide which page should be the one Google lists.",
        "Rewrite or remove the other, and redirect it if you remove it.",
    ),
    "images_missing_alt": (
        "Add alt text to each image describing what it shows.",
        "Leave alt empty only for images that are pure decoration.",
    ),
    "orphan_page": (
        "Find a related page that a visitor would come from.",
        "Add a link to this page from it.",
        "Add the page to your navigation or sitemap if it matters.",
    ),
    "ctr_below_position_baseline": (
        "Read the title and description as they appear in search results.",
        "Rewrite them to answer what the searcher is actually after.",
        "Check back in a few weeks to see whether clicks moved.",
    ),
    "striking_distance_keyword": (
        "Find the page that already ranks for this search.",
        "Cover the subject more completely on that page.",
        "Add internal links to it from related pages.",
    ),
    "declining_page": (
        "Check whether the page still answers what people are searching for.",
        "Update anything out of date, and add what is now missing.",
        "Check Search Console for which searches stopped sending traffic.",
    ),
    "cannibalisation": (
        "Decide which page should own this search.",
        "Rewrite the others to cover a different angle, or merge them.",
    ),
    "ai_crawler_blocked": (
        "Open yourdomain.com/robots.txt.",
        "Remove the rules blocking the AI crawlers you want to be visible to.",
        "Leave any blocked deliberately in place — this is your decision.",
    ),
    "no_structured_data": (
        "Add schema.org structured data describing what the page is.",
        "Most website platforms have a plugin or setting for this.",
        "Check it with Google's Rich Results Test.",
    ),
    "missing_organization_schema": (
        "Add Organization structured data to your home page.",
        "Include your name, logo, address and contact details.",
    ),
    "content_requires_js": (
        "Check what the page looks like with JavaScript switched off.",
        "Have the server send the main text in the initial HTML.",
        "Your developer or platform will know this as server-side rendering.",
    ),
}

GENERIC_FIX = ("Open the affected page and correct the problem described above.",)


def template(type_key: str) -> dict[str, Any]:
    issue_type = BY_KEY[type_key]
    return {
        "what": issue_type.title,
        "why": issue_type.summary,
        "how": list(TEMPLATE_FIXES.get(type_key, GENERIC_FIX)),
        "effort": issue_type.effort,
    }
