"""Writing `llm_calls`.

One row per MODEL CALL — never per template render, because a template render
is not a model call and counting it as one would make the refusal rate, the
cost per feature and the cache hit rate all wrong at once.

Which means the useful ratios fall straight out of this table:

    refused / total          per prompt_version — the hallucination rate
    cost_usd  by purpose     per feature unit economics
    cache_hit                whether the evidence cache is earning its keep

The accountability columns are not optional. A generation that cannot say
which provider produced it or which rows it was grounded in is rejected by a
check constraint (migration 0011) rather than stored as an orphan sentence.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection


async def record_call(
    conn: AsyncConnection,
    *,
    organization_id: UUID | None,
    website_id: UUID | None,
    purpose: str,
    prompt_version: str,
    model: str,
    model_provider: str,
    derived_from: dict[str, Any],
    status: str = "ok",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    cost_usd: Decimal | None = None,
    latency_ms: int | None = None,
    cache_hit: bool = False,
    attached_to_table: str | None = None,
    attached_to_id: str | None = None,
) -> None:
    await conn.execute(
        """
        insert into llm_calls
            (organization_id, website_id, purpose, model, model_provider,
             prompt_version, input_tokens, output_tokens, cached_input_tokens,
             cost_usd, cache_hit, latency_ms, status, derived_from,
             attached_to_table, attached_to_id)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            organization_id,
            website_id,
            purpose,
            model,
            model_provider,
            prompt_version,
            input_tokens,
            output_tokens,
            cached_input_tokens,
            cost_usd,
            cache_hit,
            latency_ms,
            status,
            json.dumps(derived_from),
            attached_to_table,
            attached_to_id,
        ),
    )
