"""Explaining issues.

The cheap, high-volume half of the AI layer. Everything expensive about it is
avoided by the cache: a generation is per PROBLEM KIND, so a site with two
hundred missing titles pays for one explanation, and the second site with the
same problem pays for none.

Order of preference, and every step of it is a normal outcome rather than an
error path:

    cache → model → template

A template is not a failure state. It is plain, accurate, hand-written advice
drawn from the issue catalogue, and a customer reading one is getting something
true. What they must never get is a generated sentence containing a number
their own data does not support — so validation failures land here too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.ai.budget import Budget, budget_for
from api.ai.cache import ExplanationCache, cache_key, render_prompt
from api.ai.metering import record_call
from api.ai.prompts import issue_explanation
from api.ai.providers import LLMProvider, ProviderError
from api.ai.validate import validate

logger = logging.getLogger("visibility_hub.ai")

PURPOSE = "issue_explanation"


@dataclass(frozen=True, slots=True)
class Explanation:
    what: str
    why: str
    how: list[str]
    effort: str
    #: 'cache' | 'model' | 'template'. Surfaced to the UI so generated prose
    #: can be labelled as generated.
    source: str
    #: Why the template was used, when it was.
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "what": self.what,
            "why": self.why,
            "how": list(self.how),
            "effort": self.effort,
            "source": self.source,
            "reason": self.reason,
        }


class ExplanationService:
    def __init__(
        self,
        conn: AsyncConnection,
        *,
        organization_id: UUID,
        website_id: UUID | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self._conn = conn
        self._organization_id = organization_id
        self._website_id = website_id
        self._provider = provider
        self._cache = ExplanationCache(conn)
        self._budget: Budget | None = None
        # Within one run the same problem kind is asked about once per issue —
        # two hundred times on a site with two hundred missing titles. Memoise
        # it, or the zero-cost cache hit becomes two hundred rows in the call
        # log and two hundred round trips to record them.
        self._seen: dict[str, Explanation] = {}

    async def budget(self) -> Budget:
        """Read once per run. A hundred explanations must not be a hundred
        aggregate queries over the call log."""
        if self._budget is None:
            self._budget = await budget_for(self._conn, self._organization_id)
        return self._budget

    async def explain(
        self, type_key: str, evidence: dict[str, Any] | None = None
    ) -> Explanation:
        payload = issue_explanation.payload_for(type_key, evidence or {})
        key = cache_key(issue_explanation.VERSION, payload)

        memoised = self._seen.get(key)
        if memoised is not None:
            return memoised

        cached = await self._cache.get(key)
        if cached is not None:
            await self._meter(
                key, type_key, cached.model, cached.model_provider, cache_hit=True
            )
            return self._remember(key, Explanation(source="cache", **cached.as_dict()))

        if self._provider is None:
            return self._remember(key, self._template(type_key, "no_provider"))

        budget = await self.budget()
        if not budget.allows(essential=False):
            return self._remember(key, self._template(type_key, "budget_exhausted"))

        try:
            generation = await self._provider.generate(
                tier=issue_explanation.TIER,
                system=issue_explanation.SYSTEM,
                prompt=render_prompt(payload),
                schema=issue_explanation.SCHEMA,
            )
        except ProviderError as exc:
            logger.warning("explanation generation failed: type=%s %s", type_key, exc)
            await self._meter(
                key, type_key, "unknown", "anthropic", status="error"
            )
            return self._remember(key, self._template(type_key, "provider_error"))

        shape_errors, invented = validate(
            generation.data, issue_explanation.SCHEMA, payload
        )
        if shape_errors or invented:
            # Logged with the offending figures but never the prose: this is
            # the measurement that makes the hallucination rate per prompt
            # version a number rather than an impression.
            logger.warning(
                "explanation refused: type=%s shape=%s figures=%s",
                type_key, shape_errors, invented,
            )
            await self._meter(
                key, type_key, generation.model, generation.model_provider,
                status="refused", generation=generation,
            )
            self._budget = None
            return self._remember(
                key,
                self._template(
                    type_key,
                    "unsupported_numbers" if invented else "validation_failed",
                ),
            )

        await self._cache.put(
            key,
            prompt_version=issue_explanation.VERSION,
            type_key=type_key,
            payload=payload,
            explanation=generation.data,
            model=generation.model,
            model_provider=generation.model_provider,
        )
        await self._meter(
            key, type_key, generation.model, generation.model_provider,
            generation=generation,
        )
        # Spend has moved, so the cached budget is stale.
        self._budget = None
        return self._remember(
            key, Explanation(source="model", **_fields(generation.data))
        )

    def _remember(self, key: str, explanation: Explanation) -> Explanation:
        self._seen[key] = explanation
        return explanation

    def _template(self, type_key: str, reason: str) -> Explanation:
        return Explanation(
            source="template",
            reason=reason,
            **_fields(issue_explanation.template(type_key)),
        )

    async def _meter(
        self,
        key: str,
        type_key: str,
        model: str,
        model_provider: str,
        *,
        status: str = "ok",
        cache_hit: bool = False,
        generation: Any = None,
    ) -> None:
        await record_call(
            self._conn,
            organization_id=self._organization_id,
            website_id=self._website_id,
            purpose=PURPOSE,
            prompt_version=issue_explanation.VERSION,
            model=model,
            model_provider=model_provider,
            derived_from={"issue_type": type_key, "cache_key": key},
            status=status,
            cache_hit=cache_hit,
            input_tokens=getattr(generation, "input_tokens", 0),
            output_tokens=getattr(generation, "output_tokens", 0),
            cached_input_tokens=getattr(generation, "cached_input_tokens", 0),
            # A cache hit costs zero, and zero is the honest figure — unlike
            # null, which this table reserves for a call whose model has no
            # price and whose true cost is therefore unknown.
            cost_usd=Decimal(0) if cache_hit else getattr(generation, "cost_usd", None),
            latency_ms=getattr(generation, "latency_ms", None),
            attached_to_table="issue_explanations",
            attached_to_id=key,
        )


def _fields(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "what": data["what"],
        "why": data["why"],
        "how": list(data["how"]),
        "effort": data["effort"],
    }
