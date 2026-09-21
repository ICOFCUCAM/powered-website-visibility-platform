"""What a model call costs.

These are configuration, not measurement. Nothing in this repository can check
them against Anthropic's published price list, so they are stated once, here,
with the date they were taken — and an unknown model prices to None rather
than to zero.

That last part matters more than the numbers. A budget that silently treats
unpriced calls as free is a budget that stops working the moment someone adds
a model, and the failure is invisible until the invoice arrives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger("visibility_hub.ai")

MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class Price:
    """US dollars per million tokens."""

    input_: Decimal
    output: Decimal
    #: Cached input is billed at a fraction of the input rate.
    cache_read_multiplier: Decimal = Decimal("0.1")


#: Checked against the published price list on 2026-09-21. A change to a rate
#: is a deliberate edit to this table, never an inference at the call site.
PRICES: dict[str, Price] = {
    "claude-opus-5": Price(Decimal(5), Decimal(25)),
    "claude-haiku-4-5-20251001": Price(Decimal(1), Decimal(5)),
}


def cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> Decimal | None:
    """None for a model with no entry, so an unpriced call is visible."""
    price = PRICES.get(model)
    if price is None:
        logger.warning("unpriced model, cost recorded as null: model=%s", model)
        return None

    billable_input = max(input_tokens - cached_input_tokens, 0)
    total = (
        Decimal(billable_input) * price.input_
        + Decimal(cached_input_tokens) * price.input_ * price.cache_read_multiplier
        + Decimal(output_tokens) * price.output
    ) / MILLION
    # Six places is what llm_calls.cost_usd stores; rounding here rather than
    # at insert keeps the in-memory total and the stored total identical.
    return total.quantize(Decimal("0.000001"))
