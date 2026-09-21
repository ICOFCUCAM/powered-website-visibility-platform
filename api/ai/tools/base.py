"""The tool framework.

THE MODEL DOES NOT WRITE SQL, and it cannot name a tenant. Both of those are
structural here rather than instructed:

  - Every tool is a parameterised query written in this repository. There is
    no tool that takes SQL, a table name, a column or an ORDER BY; the one
    place an ordering is chosen from model input, it is looked up in an
    allowlist.

  - `organization_id` and `website_id` come from `StrategistScope`, which the
    request layer builds from the authenticated session. No tool schema
    contains them, so there is no argument the model could supply to reach
    another tenant's data — and `test_strategist_tools.py` asserts that of
    every schema rather than trusting this paragraph.

A model that emits SQL against a multi-tenant database is one prompt injection
away from a cross-tenant leak, and tool results here contain page titles and
search queries: text written by strangers on the open web. So the results are
DATA. They arrive as `tool_result` blocks, never interpolated into the system
prompt, and the tools are read-only, which bounds the worst case at a wrong
answer rather than an action.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

#: The hard cap on rows any tool may return, applied after the model's own
#: `limit`. A conversation that pulls five thousand queries into the context
#: window is slow, expensive and no more useful than the top fifty.
ROW_CAP = 50

#: Google settles Search Console data a few days late; every window ends here
#: so the Strategist and the dashboard describe the same days.
GSC_LAG_DAYS = 3

#: What the model may ask for, and what each one means in days. A free-text
#: date range would be the first thing to produce "between 2019 and now" on a
#: partitioned table.
PERIODS: dict[str, int] = {
    "7d": 7,
    "28d": 28,
    "90d": 90,
    "180d": 180,
}
DEFAULT_PERIOD = "28d"

PERIOD_SCHEMA = {
    "type": "string",
    "enum": list(PERIODS),
    "description": "Window ending with the most recent settled Google data.",
}


class ToolError(Exception):
    """A bad argument. Returned to the model as a tool_result error, never
    raised at the customer: the model is expected to correct itself."""


@dataclass(frozen=True, slots=True)
class StrategistScope:
    """Who is asking and about what. Built from the session, never the model."""

    organization_id: UUID
    website_id: UUID
    domain: str
    #: Fixed once per conversation turn so every tool in that turn describes
    #: the same days. A loop that called date.today() per tool would drift
    #: across midnight and produce two windows in one answer.
    as_of: date = field(default_factory=date.today)

    def window(self, period: str | None) -> tuple[date, date]:
        days = PERIODS.get(period or DEFAULT_PERIOD)
        if days is None:
            raise ToolError(
                f"Unknown period {period!r}. Use one of: {', '.join(PERIODS)}."
            )
        end = self.as_of - timedelta(days=GSC_LAG_DAYS)
        return end - timedelta(days=days - 1), end

    def previous_window(self, period: str | None) -> tuple[date, date]:
        """The equivalent span immediately before, so a comparison is like
        for like rather than this month against last year."""
        start, _ = self.window(period)
        days = PERIODS[period or DEFAULT_PERIOD]
        end = start - timedelta(days=1)
        return end - timedelta(days=days - 1), end


Handler = Callable[[AsyncConnection, StrategistScope, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Handler
    #: A short present-tense phrase the UI shows while it runs.
    step_label: str


REGISTRY: dict[str, Tool] = {}


def tool(
    name: str, *, description: str, step_label: str, properties: dict[str, Any],
    required: list[str] | None = None,
) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        REGISTRY[name] = Tool(
            name=name,
            description=description,
            step_label=step_label,
            schema={
                "type": "object",
                "properties": properties,
                "required": required or [],
                # No stray arguments: a tool that silently ignores an unknown
                # key lets the model believe it filtered something.
                "additionalProperties": False,
            },
            handler=handler,
        )
        return handler

    return register


def definitions() -> list[dict[str, Any]]:
    """The tool list as the API expects it."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.schema,
        }
        for tool in REGISTRY.values()
    ]


def clamp(value: Any, default: int = 10) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, ROW_CAP))


@dataclass(frozen=True, slots=True)
class ToolRun:
    name: str
    input: dict[str, Any]
    result: Any
    #: None when the tool returns a single answer rather than a list. A
    #: summary reported as "0 rows" reads as "nothing came back", which is the
    #: opposite of what happened.
    rows: int | None
    ms: int
    error: str | None = None

    def as_step(self) -> dict[str, Any]:
        return {
            "tool": self.name,
            "input": self.input,
            "rows": self.rows,
            "ms": self.ms,
            "error": self.error,
        }


def _row_count(result: Any) -> int | None:
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        for value in result.values():
            if isinstance(value, list):
                return len(value)
    return None


async def run_tool(
    conn: AsyncConnection,
    scope: StrategistScope,
    name: str,
    arguments: dict[str, Any],
) -> ToolRun:
    """Run one tool. A failure is a result, not an exception.

    A tool that raises out of the loop ends the customer's turn with nothing.
    Handing the error back as a `tool_result` lets the model correct a bad
    period and carry on, which is what it usually does.
    """
    started = time.monotonic()
    tool = REGISTRY.get(name)
    if tool is None:
        return ToolRun(
            name, arguments, None, None, 0, error=f"No tool named {name}."
        )

    try:
        result = await tool.handler(conn, scope, arguments or {})
    except ToolError as exc:
        return ToolRun(
            name, arguments, None, None,
            int((time.monotonic() - started) * 1000), error=str(exc),
        )

    return ToolRun(
        name=name,
        input=arguments,
        result=result,
        rows=_row_count(result),
        ms=int((time.monotonic() - started) * 1000),
    )
