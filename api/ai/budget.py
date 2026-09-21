"""Spend admission.

Checked BEFORE the call, from `organizations.monthly_ai_budget_usd` against
this calendar month's metered spend. A limit enforced after the response has
arrived is not a limit, it is a report.

One asymmetry, straight from docs/06-ai-layer.md and worth keeping visible:

    Over budget, bulk explanations fall back to templates and the user is told
    their plan's AI allowance is used up; the weekly plan still runs, because
    it is the product.

Turning the weekly report into a template because a crawl generated too many
explanations would punish the customer for our scheduling.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_one


@dataclass(frozen=True, slots=True)
class Budget:
    limit_usd: Decimal
    spent_usd: Decimal
    #: Calls this month we could not price. An unpriced call is not free; it
    #: is unknown, and a budget with unknowns in it should say so rather than
    #: quietly under-count.
    unpriced_calls: int

    @property
    def remaining_usd(self) -> Decimal:
        return max(self.limit_usd - self.spent_usd, Decimal(0))

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.limit_usd

    def allows(self, *, essential: bool) -> bool:
        """`essential` is the weekly plan. Everything else is bulk."""
        return essential or not self.exhausted


async def budget_for(conn: AsyncConnection, organization_id: UUID) -> Budget:
    row = await fetch_one(
        conn,
        """
        select o.monthly_ai_budget_usd as limit_usd,
               coalesce(sum(c.cost_usd), 0) as spent_usd,
               count(*) filter (
                   where c.id is not null and c.cost_usd is null
               ) as unpriced
          from organizations o
          left join llm_calls c
                 on c.organization_id = o.id
                and c.status <> 'error'
                and c.created_at >= date_trunc('month', now())
         where o.id = %s
         group by o.monthly_ai_budget_usd
        """,
        (organization_id,),
    )
    if row is None:
        # No organisation, no allowance. Reached only by a caller that has
        # already lost its tenant scope, so it must not default to generous.
        return Budget(Decimal(0), Decimal(0), 0)
    return Budget(
        limit_usd=Decimal(row["limit_usd"]),
        spent_usd=Decimal(row["spent_usd"]),
        unpriced_calls=int(row["unpriced"]),
    )
