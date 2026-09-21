"""The numbers validator.

Rule 1 of the AI layer (docs/06-ai-layer.md): *the model never invents a
number*. This module is the enforcement, and it is the cheapest useful check
in the whole system — extract every figure from a generation, and assert that
each one was in the evidence the model was given.

It does not check that the model used the numbers *correctly*; nothing
automatic can. It checks that every figure a customer reads came from their
own data, which is the failure mode that turns a helpful product into a
liability: a fabricated "2,400 searches a month" is indistinguishable from a
real one to the person acting on it.

Two things it deliberately tolerates:

  - Rounding. Evidence holds a CTR of 0.007712; the prose says 0.77%. Both are
    the same fact, and demanding an exact match would reject every honest
    sentence a model writes.
  - Numbers spelled as words. "two pages" is not checked, because the regex
    does not see it. That is a real gap, and it is a much smaller one than it
    looks: fabricated statistics are written as digits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

#: A number, optionally with thousands separators, decimals and a trailing %.
#:
#: The leading guard rejects a digit that is part of a word or a version
#: string: `H1`, `GA4`, `utm_2` and `1.2.3` are identifiers, not claims, and
#: treating them as figures produced false refusals in every early draft.
#:
#: The trailing guard rejects only a CONTINUATION of the number — another word
#: character, or a further decimal group. It must not reject a full stop, or
#: "you had 2,400." would end a sentence and slip past the check entirely,
#: which is the one direction this regex is not allowed to be wrong in.
_NUMBER = re.compile(
    r"""
    (?<![\w.])
    (?P<int>\d{1,3}(?:,\d{3})+ | \d+)
    (?:\.(?P<frac>\d+))?
    (?!\w) (?!\.\d)
    \s*(?P<pct>%)?
    """,
    re.VERBOSE,
)

#: Numeric substrings anywhere in evidence text, so a date of 2026-09-21 makes
#: 2026, 21 and 9 quotable, and a URL ending /page-2 makes 2 quotable.
_ANY_DIGITS = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class Figure:
    raw: str
    value: Decimal
    decimals: int
    is_percent: bool


def figures_in(text: str) -> list[Figure]:
    found: list[Figure] = []
    for match in _NUMBER.finditer(text):
        whole = match.group("int").replace(",", "")
        frac = match.group("frac") or ""
        try:
            value = Decimal(f"{whole}.{frac}" if frac else whole)
        except InvalidOperation:  # pragma: no cover - regex forbids it
            continue
        found.append(
            Figure(
                raw=match.group(0).strip(),
                value=value,
                decimals=len(frac),
                is_percent=match.group("pct") is not None,
            )
        )
    return found


def supported_values(evidence: Any) -> set[Decimal]:
    """Every number anywhere in the evidence, including inside strings."""
    values: set[Decimal] = set()

    def walk(node: Any) -> None:
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, (int, float, Decimal)):
            values.add(Decimal(str(node)))
        elif isinstance(node, str):
            for token in _ANY_DIGITS.findall(node):
                values.add(Decimal(token))
        elif isinstance(node, dict):
            for key, value in node.items():
                # Keys can carry figures too: {"position_3_to_10": 14}.
                walk(key)
                walk(value)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item)

    walk(evidence)
    return values


def _round(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places))


def is_supported(figure: Figure, values: set[Decimal]) -> bool:
    for value in values:
        try:
            if _round(value, figure.decimals) == figure.value:
                return True
            # A rate in the evidence, rendered as a percentage in the prose.
            if abs(value) <= 1 and _round(value * 100, figure.decimals) == figure.value:
                return True
        except InvalidOperation:  # pragma: no cover - absurdly large evidence
            continue
    return False


def texts_in(generated: Any) -> list[str]:
    """Every string in a generated structure, so nested prose is checked too."""
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(generated)
    return out


def unsupported_figures(generated: Any, evidence: Any) -> list[str]:
    """The figures a customer would read that their own data does not contain.

    Empty means the generation may be stored. Anything else is a refusal.
    """
    values = supported_values(evidence)
    unsupported: list[str] = []
    for text in texts_in(generated):
        for figure in figures_in(text):
            if not is_supported(figure, values):
                unsupported.append(figure.raw)
    return unsupported
