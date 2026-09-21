"""The numbers validator.

Rule 1 — the model never invents a number — is the only guardrail in the AI
layer whose failure is silently plausible: a fabricated "2,400 searches a
month" looks exactly like a real one to the person acting on it.

So these tests come in two halves. The refusals matter, and so do the
acceptances: a validator that rejects honest rounding would push every
generation to the template and quietly turn the AI layer off.
"""

from __future__ import annotations

from api.ai.numbers import figures_in, unsupported_figures
from api.ai.validate import schema_errors, validate

EVIDENCE = {
    "clicks": 1234,
    "impressions": 48219,
    "ctr": 0.0077123,
    "position": 3.1998,
    "pages_affected": 12,
    "period": {"start": "2026-09-01", "end": "2026-09-28"},
    "example_urls": ["https://example.com/blog/page-2"],
}


def unsupported(text: str) -> list[str]:
    return unsupported_figures({"why": text}, EVIDENCE)


# -- what must pass ---------------------------------------------------------
def test_a_figure_quoted_exactly_is_supported():
    assert unsupported("You had 1,234 clicks from 48,219 impressions.") == []


def test_a_rate_rendered_as_a_percentage_is_supported():
    """0.0077123 in the evidence, 0.77% in the prose. Same fact."""
    assert unsupported("Your click-through rate is 0.77%.") == []
    assert unsupported("Your click-through rate is 0.8%.") == []


def test_a_rounded_figure_is_supported():
    assert unsupported("You sit at position 3.2 on average.") == []
    assert unsupported("You sit at position 3.") == []


def test_a_figure_at_the_end_of_a_sentence_is_still_checked():
    """The one direction the regex may not be wrong in.

    A trailing full stop must not make the number invisible to the check, or
    "it gets 2,400." would end a sentence and slip past entirely.
    """
    assert unsupported("It already gets 1,234.") == []
    assert unsupported("It gets 2,400.") == ["2,400"]


def test_dates_in_the_evidence_may_be_quoted():
    assert unsupported("Between 1 September and 28 September 2026.") == []


def test_identifiers_are_not_treated_as_claims():
    """H1, GA4 and a version string are names, not figures. Reading them as
    claims made every early draft refuse honest sentences."""
    assert unsupported("Your H1 is missing, GA4 is connected, see v1.2.3.") == []


def test_numbers_inside_evidence_strings_count():
    assert unsupported("The example is /blog/page-2.") == []


# -- what must fail ---------------------------------------------------------
def test_an_invented_search_volume_is_refused():
    assert unsupported("This phrase gets about 2,400 searches a month.") == [
        "2,400"
    ]


def test_an_invented_percentage_is_refused():
    assert unsupported("Expect a 35% lift in traffic.") == ["35%"]


def test_a_figure_is_checked_everywhere_in_the_structure():
    """Nested prose is prose. A fabricated number inside step three of `how`
    reaches the customer exactly as readily as one in the summary."""
    generated = {
        "what": "Your titles are missing.",
        "why": "This affects 12 pages.",
        "how": ["Write a title.", "Aim for 9,900 monthly searches."],
        "effort": "low",
    }
    assert unsupported_figures(generated, EVIDENCE) == ["9,900"]


def test_every_unsupported_figure_is_reported_not_just_the_first():
    assert unsupported("It gets 2,400 searches and 15,000 visits.") == [
        "2,400",
        "15,000",
    ]


# -- the schema half --------------------------------------------------------
SCHEMA = {
    "type": "object",
    "properties": {
        "what": {"type": "string"},
        "how": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "effort": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["what", "how", "effort"],
    "additionalProperties": False,
}


def test_a_missing_field_is_an_error():
    assert schema_errors({"how": ["x"], "effort": "low"}, SCHEMA) == [
        "output: missing required field 'what'"
    ]


def test_a_value_outside_the_enum_is_an_error():
    errors = schema_errors({"what": "x", "how": ["y"], "effort": "trivial"}, SCHEMA)
    assert errors == ["output.effort: 'trivial' is not one of ['low', 'medium', 'high']"]


def test_an_empty_list_fails_min_items():
    errors = schema_errors({"what": "x", "how": [], "effort": "low"}, SCHEMA)
    assert errors == ["output.how: expected at least 1 items"]


def test_an_extra_field_is_an_error():
    errors = schema_errors(
        {"what": "x", "how": ["y"], "effort": "low", "confidence": 0.9}, SCHEMA
    )
    assert errors == ["output: unexpected field 'confidence'"]


def test_a_boolean_is_not_an_integer():
    """True is an int in Python and is never what an integer field meant."""
    assert schema_errors(True, {"type": "integer"}) == ["output: expected an integer"]


def test_validate_returns_both_kinds_of_problem():
    shape, invented = validate(
        {"what": "x", "how": ["gets 900 searches"], "effort": "nope"},
        SCHEMA,
        EVIDENCE,
    )
    assert shape and invented == ["900"]


def test_figures_are_extracted_with_their_original_text():
    """The refusal log quotes what the model wrote, not a normalised value."""
    assert [f.raw for f in figures_in("1,234 clicks at 0.77 %")] == [
        "1,234",
        "0.77 %",
    ]
