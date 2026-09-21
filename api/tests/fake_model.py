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





# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------
@dataclass
class Scripted:
    """One model turn: some text, and optionally some tool calls."""

    text: str = ""
    tools: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    stop_reason: str | None = None


@dataclass
class FakeChatProvider:
    """Plays a script. Records the messages and tools it was handed.

    The recorded requests are the point of most of the tests that use it: what
    the loop sends on the last round, whether tool results came back as
    `tool_result` blocks, and whether history was replayed as text.
    """

    turns: list[Any] = field(default_factory=list)
    error: Exception | None = None
    model: str = "claude-opus-5"
    requests: list[dict[str, Any]] = field(default_factory=list)

    def stream(self, *, system, messages, tools):
        from api.ai.providers import TextDelta, ToolCall, TurnFinished

        index = len(self.requests)
        self.requests.append(
            {
                "system": system,
                "messages": [dict(m) for m in messages],
                "tools": [t["name"] for t in tools],
            }
        )

        async def generate():
            if self.error is not None:
                raise self.error
            turn = (
                self.turns[index]
                if index < len(self.turns)
                else Scripted(text="I don't have anything more to add.")
            )
            if isinstance(turn, Exception):
                raise turn

            # Delivered in fragments, because a caller that only works when
            # the whole answer arrives at once is not a streaming caller.
            for word in turn.text.split(" "):
                if word:
                    yield TextDelta(word + " ")

            calls = [
                ToolCall(id=f"toolu_{index}_{n}", name=name, input=args)
                for n, (name, args) in enumerate(turn.tools)
            ]
            yield TurnFinished(
                text=turn.text,
                tool_calls=calls,
                stop_reason=turn.stop_reason
                or ("tool_use" if calls else "end_turn"),
                model=self.model,
                input_tokens=1500,
                output_tokens=200,
                cached_input_tokens=0,
                cost_usd=Decimal("0.0125"),
                content=[{"type": "text", "text": turn.text}],
            )

        return generate()


def sse_stream(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events
    ).encode()


def streamed_message(
    text: str = "",
    tool: tuple[str, dict[str, Any]] | None = None,
    *,
    model: str = "claude-opus-5",
    stop_reason: str | None = None,
    input_tokens: int = 1500,
    output_tokens: int = 200,
) -> bytes:
    """A real SSE message stream, as the API emits one."""
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_stream",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": 1,
                    },
                },
            },
        )
    ]
    index = 0
    if text:
        events += [
            ("content_block_start",
             {"type": "content_block_start", "index": index,
              "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta",
             {"type": "content_block_delta", "index": index,
              "delta": {"type": "text_delta", "text": text}}),
            ("content_block_stop", {"type": "content_block_stop", "index": index}),
        ]
        index += 1
    if tool is not None:
        name, arguments = tool
        events += [
            ("content_block_start",
             {"type": "content_block_start", "index": index,
              "content_block": {"type": "tool_use", "id": "toolu_1",
                                "name": name, "input": {}}}),
            ("content_block_delta",
             {"type": "content_block_delta", "index": index,
              "delta": {"type": "input_json_delta",
                        "partial_json": json.dumps(arguments)}}),
            ("content_block_stop", {"type": "content_block_stop", "index": index}),
        ]
    events += [
        ("message_delta",
         {"type": "message_delta",
          "delta": {"stop_reason": stop_reason
                    or ("tool_use" if tool else "end_turn"),
                    "stop_sequence": None},
          "usage": {"output_tokens": output_tokens}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return sse_stream(events)


def anthropic_chat_over(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> tuple[Any, list[dict[str, Any]]]:
    """The real streaming provider over a mock transport."""
    from anthropic import AsyncAnthropic

    from api.ai.providers import AnthropicChatProvider

    seen: list[dict[str, Any]] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return handler(request)

    client = AsyncAnthropic(
        api_key="test-key-not-real",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(record)),
    )
    return AnthropicChatProvider(client), seen


__all__ = [
    "FakeChatProvider",
    "FakeProvider",
    "ProviderError",
    "Recorded",
    "Scripted",
    "anthropic_chat_over",
    "anthropic_over",
    "message_response",
    "sse_stream",
    "streamed_message",
]
