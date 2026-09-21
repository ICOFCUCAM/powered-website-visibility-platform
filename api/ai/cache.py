"""The generation cache, keyed on the prompt.

The key is a hash of the exact text sent to the model, which makes the
cache-correctness property structural rather than remembered: there is no way
to add a field to the prompt and forget to add it to the key, because the
prompt IS the canonicalised payload.

That also makes the cross-tenant sharing safe. Two organisations collide only
when the text each would have been shown is byte-identical, so a shared row
cannot carry one tenant's data to another. See the comment on the table in
migration 0017.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from api.adapters.db import fetch_one


def canonical(payload: Any) -> str:
    """Stable JSON: sorted keys, no insignificant whitespace.

    Without sort_keys the same facts in a different dict order would miss the
    cache, which is a bug that costs money rather than correctness and so
    would take a billing cycle to notice.
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def render_prompt(payload: Any) -> str:
    return canonical(payload)


def cache_key(prompt_version: str, payload: Any) -> str:
    digest = hashlib.sha256()
    digest.update(prompt_version.encode())
    digest.update(b"\n")
    digest.update(render_prompt(payload).encode())
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class CachedExplanation:
    cache_key: str
    what: str
    why: str
    how: list[str]
    effort: str
    model: str
    model_provider: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "what": self.what,
            "why": self.why,
            "how": list(self.how),
            "effort": self.effort,
        }

    @property
    def generated(self) -> bool:
        return self.model_provider != "template"


class ExplanationCache:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def get(self, key: str) -> CachedExplanation | None:
        row = await fetch_one(
            self._conn,
            """
            update issue_explanations
               set hit_count = hit_count + 1, last_used_at = now()
             where cache_key = %s
            returning cache_key, what, why, how, effort, model, model_provider
            """,
            (key,),
        )
        if row is None:
            return None
        return CachedExplanation(
            cache_key=row["cache_key"],
            what=row["what"],
            why=row["why"],
            how=list(row["how"] or []),
            effort=row["effort"],
            model=row["model"],
            model_provider=row["model_provider"],
        )

    async def put(
        self,
        key: str,
        *,
        prompt_version: str,
        type_key: str,
        payload: dict[str, Any],
        explanation: dict[str, Any],
        model: str,
        model_provider: str,
    ) -> None:
        """Last writer wins, deliberately.

        A concurrent crawl of two sites with the same problem will race here.
        Both rows say the same thing about the same evidence, so there is
        nothing to reconcile and nothing to lock.
        """
        await self._conn.execute(
            """
            insert into issue_explanations
                (cache_key, prompt_version, type_key, what, why, how, effort,
                 model_provider, model, evidence)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (cache_key) do update
               set what = excluded.what, why = excluded.why,
                   how = excluded.how, effort = excluded.effort,
                   model = excluded.model,
                   model_provider = excluded.model_provider,
                   generated_at = now()
            """,
            (
                key,
                prompt_version,
                type_key,
                explanation["what"],
                explanation["why"],
                list(explanation["how"]),
                explanation["effort"],
                model_provider,
                model,
                json.dumps(payload),
            ),
        )
