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
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters import db

#: How a caller gets a connection to write the spend log with.
MeterSession = Callable[[], AbstractAsyncContextManager[AsyncConnection]]

#: THE SPEND LOG IS WRITTEN BY THE SYSTEM, NEVER BY A CLIENT ROLE.
#:
#: `llm_calls` carries a read policy and no write policy on purpose (migration
#: 0016): a customer may inspect their own metering, and nothing reachable from
#: a browser may add to it. A client role that could insert rows could inflate
#: its own recorded spend and pollute the cost-per-feature figures the pricing
#: decisions come from.
#:
#: That matters here because the Strategist and the "generate my report now"
#: button both meter from the REQUEST path, where the connection is the
#: RLS-bound `app_user`. So metering opens its own service-role session rather
#: than borrowing the caller's, and the tenant values it writes come from a
#: scope the session produced — never from anything the client sent.
DEFAULT_SESSION: MeterSession = db.service_session


@asynccontextmanager
async def _reuse(conn: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    yield conn


def reusing(conn: AsyncConnection) -> MeterSession:
    """For a caller that already holds a service connection — a nightly job,
    or a test — so metering does not open a second one."""
    return lambda: _reuse(conn)


async def record_call(
    session: MeterSession,
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
    async with session() as conn:
        await _insert(
            conn,
            organization_id=organization_id,
            website_id=website_id,
            purpose=purpose,
            prompt_version=prompt_version,
            model=model,
            model_provider=model_provider,
            derived_from=derived_from,
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
            attached_to_table=attached_to_table,
            attached_to_id=attached_to_id,
        )


async def _insert(
    conn: AsyncConnection,
    *,
    organization_id: UUID | None,
    website_id: UUID | None,
    purpose: str,
    prompt_version: str,
    model: str,
    model_provider: str,
    derived_from: dict[str, Any],
    status: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    cost_usd: Decimal | None,
    latency_ms: int | None,
    cache_hit: bool,
    attached_to_table: str | None,
    attached_to_id: str | None,
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
