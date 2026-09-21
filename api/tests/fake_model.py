"""A model that never leaves the process.

Two fakes, doing different jobs:

  `FakeProvider` stands in for the whole provider so a test about caching,
  budgets or plan ranking does not have to care what an SDK response looks
  like.

  `anthropic_over` runs the REAL `AnthropicProvider` against a mock HTTP
  transport, so the request shape — model id, thinking mode, structured output
  — is exercised by the same code that will talk to Anthropic in production.
  A provider tested only through a stub is a provider nobody has tested.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx2

from api.ai.providers import AnthropicProvider, Generation, ProviderError


@dataclass
class Recorded:
    tier: str
    system: str
    prompt: str
    schema: dict[str, Any]

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.prompt)


@dataclass
class FakeProvider:
    """Returns what it is told to. Records what it was asked."""

    #: A dict, a list consumed one call at a time, or a callable over the
    #: parsed payload.
    responses: Any = field(default_factory=dict)
    error: Exception | None = None
    model: str = "fake-model"
    cost: Decimal | None = Decimal("0.001")
    calls: list[Recorded] = field(default_factory=list)

    async def generate(
        self, *, tier: str, system: str, prompt: str, schema: dict[str, Any]
    ) -> Generation:
        self.calls.append(Recorded(tier, system, prompt, schema))
        if self.error is not None:
            raise self.error

        source = self.responses
        if callable(source):
            data = source(json.loads(prompt))
        elif isinstance(source, list):
            data = source[min(len(self.calls) - 1, len(source) - 1)]
        else:
            data = source
        if isinstance(data, Exception):
            raise data

        return Generation(
            data=data,
            model=self.model,
            model_provider="anthropic",
            input_tokens=1200,
            output_tokens=300,
            cached_input_tokens=0,
            latency_ms=42,
            cost_usd=self.cost,
        )


def message_response(
    data: Any,
    *,
    model: str = "claude-opus-5",
    stop_reason: str = "end_turn",
    input_tokens: int = 1200,
    output_tokens: int = 300,
    cache_read_input_tokens: int | None = None,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cache_read_input_tokens is not None:
        usage["cache_read_input_tokens"] = cache_read_input_tokens
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {
                "type": "text",
                "text": data if isinstance(data, str) else json.dumps(data),
            }
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


def anthropic_over(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> tuple[AnthropicProvider, list[dict[str, Any]]]:
    """The real provider over a mock transport, plus the requests it made."""
    from anthropic import AsyncAnthropic

    seen: list[dict[str, Any]] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return handler(request)

    client = AsyncAnthropic(
        api_key="test-key-not-real",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(record)),
    )
    return AnthropicProvider(client), seen


__all__ = [
    "FakeProvider",
    "ProviderError",
    "Recorded",
    "anthropic_over",
    "message_response",
]
