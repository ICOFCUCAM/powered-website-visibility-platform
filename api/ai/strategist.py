"""The AI Strategist.

A conversation over the customer's own data, and the loop that makes it one.

Three things bound it, all of them here rather than in the prompt, because a
prompt is a request and a loop is a rule:

  TOOL BUDGET      at most `MAX_TOOL_CALLS` tool calls per customer message.
  ROUND BUDGET     at most `MAX_ROUNDS` model turns, and the last one is sent
                   WITHOUT tools — so a model that would happily keep looking
                   is made to answer instead of stopping silently.
  SPEND BUDGET     checked before the first round and metered after every one.

What the model may look at is the nine typed tools in `api/ai/tools`. It never
sees a connection, a credential, an organisation id or a website id: the scope
comes from the session and is bound into every query server-side.

History is replayed as text. The tool traffic of previous turns is recorded in
`conversation_messages.steps` for audit but is not re-sent: it would balloon
the context, and last week's figures are not this week's.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all, fetch_one
from api.ai.budget import budget_for
from api.ai.metering import DEFAULT_SESSION, MeterSession, record_call
from api.ai.prompts import strategist as prompt
from api.ai.providers import (
    ChatProvider,
    ProviderError,
    ProviderRefused,
    TextDelta,
    ToolCall,
    TurnFinished,
)
from api.ai.tools import REGISTRY, StrategistScope, definitions, run_tool

logger = logging.getLogger("visibility_hub.ai")

PURPOSE = "strategist_chat"

#: A question longer than this is not a question. Cutting it here keeps one
#: paste of a log file from costing a month's allowance.
MAX_QUESTION_CHARS = 2000

#: What a tool result may contribute to the context. Row caps already bound
#: the shape; this bounds the bytes, because a page's meta description is
#: attacker-controlled text of arbitrary length.
MAX_TOOL_RESULT_CHARS = 20_000


class StrategistUnavailable(RuntimeError):
    """No model is configured. There is no template answer for a conversation,
    and pretending otherwise would be worse than saying so."""


@dataclass
class Answer:
    text: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    stop_reason: str | None = None
    message_id: int | None = None


def _truncate(payload: Any) -> str:
    text = json.dumps(payload, default=str)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS] + '… (truncated)"'


class Strategist:
    def __init__(
        self,
        conn: AsyncConnection,
        *,
        scope: StrategistScope,
        provider: ChatProvider | None,
        user_id: UUID | None = None,
        meter: MeterSession = DEFAULT_SESSION,
    ) -> None:
        self._conn = conn
        self._meter_session = meter
        self._scope = scope
        self._provider = provider
        self._user_id = user_id

    # -- conversations ----------------------------------------------------
    async def conversation(self, conversation_id: UUID | None, question: str) -> UUID:
        """Find the conversation, or open one named after the first question."""
        if conversation_id is not None:
            row = await fetch_one(
                self._conn,
                "select id from conversations where id = %s and website_id = %s",
                (conversation_id, self._scope.website_id),
            )
            if row is None:
                raise LookupError("conversation not found")
            return row["id"]

        row = await fetch_one(
            self._conn,
            """
            insert into conversations (organization_id, website_id, created_by,
                                       title)
            values (%s,%s,%s,%s) returning id
            """,
            (
                self._scope.organization_id,
                self._scope.website_id,
                self._user_id,
                question[:80],
            ),
        )
        return row["id"]

    async def _history(self, conversation_id: UUID) -> list[dict[str, Any]]:
        rows = await fetch_all(
            self._conn,
            """
            select role, content from conversation_messages
             where conversation_id = %s and content <> ''
             order by seq desc limit %s
            """,
            (conversation_id, prompt.HISTORY_TURNS),
        )
        return [
            {"role": row["role"], "content": row["content"]}
            for row in reversed(rows)
        ]

    async def _append(
        self,
        conversation_id: UUID,
        *,
        role: str,
        content: str,
        steps: list[dict[str, Any]] | None = None,
        model: str | None = None,
        stop_reason: str | None = None,
    ) -> int:
        row = await fetch_one(
            self._conn,
            """
            insert into conversation_messages
                (organization_id, conversation_id, seq, role, content, steps,
                 model, model_provider, prompt_version, stop_reason)
            values (%s, %s,
                    (select coalesce(max(seq), 0) + 1 from conversation_messages
                      where conversation_id = %s),
                    %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                self._scope.organization_id,
                conversation_id,
                conversation_id,
                role,
                content,
                json.dumps(steps or []),
                model,
                "anthropic" if model else None,
                prompt.VERSION,
                stop_reason,
            ),
        )
        await self._conn.execute(
            "update conversations set message_count = message_count + 1, "
            "       last_message_at = now() where id = %s",
            (conversation_id,),
        )
        return row["id"]

    # -- the loop ---------------------------------------------------------
    async def ask(
        self, *, conversation_id: UUID | None, question: str
    ) -> AsyncIterator[dict[str, Any]]:
        question = (question or "").strip()[:MAX_QUESTION_CHARS]
        if not question:
            yield _error("empty_question", "Ask me something about your website.")
            return

        if self._provider is None:
            raise StrategistUnavailable(
                "The assistant isn't available on this installation yet."
            )

        budget = await budget_for(self._conn, self._scope.organization_id)
        if not budget.allows(essential=False):
            yield _error(
                "ai_budget_exhausted",
                "You've used this month's AI allowance. Your dashboard, audit "
                "and weekly plan are unaffected.",
            )
            return

        conversation_id = await self.conversation(conversation_id, question)
        yield {"type": "conversation", "id": str(conversation_id)}

        history = await self._history(conversation_id)
        await self._append(conversation_id, role="user", content=question)

        messages: list[dict[str, Any]] = [
            *history,
            {"role": "user", "content": question},
        ]
        answer = Answer()
        calls_left = prompt.MAX_TOOL_CALLS

        for round_number in range(1, prompt.MAX_ROUNDS + 1):
            # The final round goes out without tools. A model that has spent
            # its budget still owes the customer a sentence, and "no tools"
            # is how you ask for one.
            last_round = round_number == prompt.MAX_ROUNDS or calls_left <= 0
            tools = [] if last_round else definitions()

            try:
                finished = None
                async for event in self._provider.stream(
                    system=prompt.system_for(self._scope.domain),
                    messages=messages,
                    tools=tools,
                ):
                    if isinstance(event, TextDelta):
                        answer.text += event.text
                        yield {"type": "delta", "text": event.text}
                    elif isinstance(event, TurnFinished):
                        finished = event
            except ProviderRefused:
                logger.info("strategist refused a question")
                await self._meter(status="refused")
                yield _error(
                    "assistant_declined",
                    "I can't answer that one. Try asking about your website's "
                    "traffic, pages or problems.",
                )
                return
            except ProviderError as exc:
                logger.warning("strategist provider failed: %s", exc)
                await self._meter(status="error")
                yield _error(
                    "assistant_unavailable",
                    "I couldn't reach the assistant just now. Please try again.",
                )
                return

            if finished is None:  # pragma: no cover - provider contract
                yield _error("assistant_unavailable", "The assistant stopped early.")
                return

            answer.model = finished.model
            answer.stop_reason = finished.stop_reason
            await self._meter(turn=finished, tools=[c.name for c in finished.tool_calls])

            if not finished.tool_calls:
                break

            results: list[dict[str, Any]] = []
            for call in finished.tool_calls:
                if calls_left <= 0:
                    results.append(_budget_spent(call))
                    continue
                calls_left -= 1
                async for event in self._run(call, answer, results):
                    yield event

            messages.append({"role": "assistant", "content": finished.content})
            messages.append({"role": "user", "content": results})

            # Text written before a tool call is part of the answer the
            # customer watched arrive, so keep it and separate the rounds.
            if answer.text and not answer.text.endswith("\n\n"):
                answer.text += "\n\n"

        answer.text = answer.text.strip()
        answer.message_id = await self._append(
            conversation_id,
            role="assistant",
            content=answer.text,
            steps=answer.steps,
            model=answer.model,
            stop_reason=answer.stop_reason,
        )
        yield {
            "type": "done",
            "conversation_id": str(conversation_id),
            "message_id": answer.message_id,
            "steps": answer.steps,
        }

    async def _run(
        self, call: ToolCall, answer: Answer, results: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        tool = REGISTRY.get(call.name)
        yield {
            "type": "step",
            "tool": call.name,
            "label": tool.step_label if tool else "checking",
            "input": call.input,
        }

        run = await run_tool(self._conn, self._scope, call.name, call.input)
        answer.steps.append(run.as_step())
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": run.error or _truncate(run.result),
                **({"is_error": True} if run.error else {}),
            }
        )

    async def _meter(
        self,
        *,
        turn: TurnFinished | None = None,
        tools: list[str] | None = None,
        status: str = "ok",
    ) -> None:
        await record_call(
            self._meter_session,
            organization_id=self._scope.organization_id,
            website_id=self._scope.website_id,
            purpose=PURPOSE,
            prompt_version=prompt.VERSION,
            model=turn.model if turn else "unknown",
            model_provider="anthropic",
            # A chat turn is not an explanation OF a row, so it carries the
            # tools it consulted instead. `llm_calls` exempts this purpose
            # from the grounding constraint for exactly that reason (0011).
            derived_from={"tools": tools or []},
            status=status,
            input_tokens=turn.input_tokens if turn else 0,
            output_tokens=turn.output_tokens if turn else 0,
            cached_input_tokens=turn.cached_input_tokens if turn else 0,
            cost_usd=turn.cost_usd if turn else None,
            attached_to_table="conversation_messages",
        )


def _budget_spent(call: ToolCall) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": call.id,
        "is_error": True,
        "content": (
            "Tool budget for this question is spent. Answer now with what you "
            "already have, and say what you could not check."
        ),
    }


def _error(code: str, message: str) -> dict[str, Any]:
    return {"type": "error", "code": code, "message": message}
