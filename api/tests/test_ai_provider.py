"""The Anthropic provider, exercised through the real SDK.

The transport is a mock; everything above it — request construction, model
choice, thinking mode, structured output, usage accounting, refusal handling —
is the code that will run against Anthropic. A provider verified only through
a stub has never had its request shape checked by anything.
"""

from __future__ import annotations

import json

import httpx2
import pytest

from api.ai.providers import (
    MAX_OUTPUT_TOKENS,
    ProviderError,
    ProviderRefused,
)
from api.tests.fake_model import anthropic_over, message_response

SCHEMA = {
    "type": "object",
    "properties": {"what": {"type": "string"}},
    "required": ["what"],
    "additionalProperties": False,
}


def responds_stream(body: bytes):
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    return handler


def responds(body, status: int = 200):
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=body)

    return handler


async def generate(provider, tier="frontier"):
    return await provider.generate(
        tier=tier, system="You explain things.", prompt='{"a":1}', schema=SCHEMA
    )


async def test_the_frontier_tier_asks_for_the_frontier_model_with_thinking():
    provider, requests = anthropic_over(responds(message_response({"what": "hi"})))
    await generate(provider)

    sent = requests[0]
    assert sent["model"] == "claude-opus-5"
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["max_tokens"] == MAX_OUTPUT_TOKENS
    assert sent["output_config"]["format"]["schema"] == SCHEMA


async def test_the_cheap_tier_sends_no_thinking_block():
    """Bulk explanation is formatting over evidence the rules engine has
    already reasoned about. Thinking tokens on it are spend without a
    product, and `budget_tokens` would be rejected outright."""
    provider, requests = anthropic_over(
        responds(message_response({"what": "hi"}, model="claude-haiku-4-5-20251001"))
    )
    await generate(provider, tier="cheap")

    sent = requests[0]
    assert sent["model"] == "claude-haiku-4-5-20251001"
    assert "thinking" not in sent
    assert "budget_tokens" not in json.dumps(sent)


async def test_usage_and_cost_come_back_with_the_generation():
    provider, _ = anthropic_over(
        responds(
            message_response(
                {"what": "hi"}, input_tokens=10_000, output_tokens=2_000
            )
        )
    )
    generation = await generate(provider)

    assert generation.data == {"what": "hi"}
    assert (generation.input_tokens, generation.output_tokens) == (10_000, 2_000)
    # 10k input at $5/MTok + 2k output at $25/MTok.
    assert str(generation.cost_usd) == "0.100000"


async def test_cached_input_is_billed_at_the_cache_rate():
    """Cache reads are a tenth of the input rate, and counting them at full
    price would make prompt caching look like it saved nothing."""
    provider, _ = anthropic_over(
        responds(
            message_response(
                {"what": "hi"},
                input_tokens=1_000,
                output_tokens=0,
                cache_read_input_tokens=9_000,
            )
        )
    )
    generation = await generate(provider)

    assert generation.cached_input_tokens == 9_000
    assert generation.input_tokens == 10_000
    # 1k at $5/MTok plus 9k at $0.50/MTok.
    assert str(generation.cost_usd) == "0.009500"


async def test_an_unpriced_model_records_no_cost_rather_than_zero():
    provider, _ = anthropic_over(
        responds(message_response({"what": "hi"}, model="some-future-model"))
    )
    assert (await generate(provider)).cost_usd is None


async def test_a_refusal_is_raised_before_the_content_is_read():
    """On a refusal the content is not an answer, and parsing it as one is
    how a refusal becomes a stored recommendation."""
    body = message_response({"what": "hi"}, stop_reason="refusal")
    provider, _ = anthropic_over(responds(body))

    with pytest.raises(ProviderRefused):
        await generate(provider)


async def test_a_truncated_generation_is_an_error_not_a_partial_answer():
    provider, _ = anthropic_over(
        responds(message_response('{"what": "hi', stop_reason="max_tokens"))
    )
    with pytest.raises(ProviderError, match="truncated"):
        await generate(provider)


async def test_unparseable_output_is_an_error():
    provider, _ = anthropic_over(responds(message_response("not json at all")))
    with pytest.raises(ProviderError, match="unparseable"):
        await generate(provider)


async def test_the_text_block_is_found_past_a_thinking_block():
    body = message_response({"what": "hi"})
    body["content"] = [
        {"type": "thinking", "thinking": "...", "signature": "x"}
    ] + body["content"]
    provider, _ = anthropic_over(responds(body))

    assert (await generate(provider)).data == {"what": "hi"}


async def test_a_model_without_structured_output_is_retried_once_without_it():
    """A model that does not accept `output_config` is a configuration fact,
    not a transient failure. Our own validation runs on either path, so the
    retry gets the same content through the text block."""
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        body = json.loads(request.content)
        if "output_config" in body:
            return httpx2.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "output_config is not supported by this model",
                    },
                },
            )
        return httpx2.Response(200, json=message_response({"what": "hi"}))

    provider, requests = anthropic_over(handler)
    generation = await generate(provider, tier="cheap")

    assert generation.data == {"what": "hi"}
    assert len(calls) == 2
    # The schema has to survive the retry, or the second request is
    # unconstrained in both directions.
    assert json.loads(
        requests[1]["system"].split("schema exactly:\n")[1]
    ) == SCHEMA


async def test_any_other_bad_request_is_not_retried():
    """One retry, and only for the one thing worth retrying. Everything else
    goes straight to the caller's template fallback rather than spending a
    second call to fail again."""
    provider, requests = anthropic_over(
        responds(
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "bad model"},
            },
            status=400,
        )
    )
    with pytest.raises(ProviderError):
        await generate(provider)
    assert len(requests) == 1


async def test_a_server_error_becomes_a_provider_error():
    provider, _ = anthropic_over(
        responds({"type": "error", "error": {"message": "overloaded"}}, status=529)
    )
    with pytest.raises(ProviderError):
        await generate(provider)


# ---------------------------------------------------------------------------
# Streaming conversation
# ---------------------------------------------------------------------------
async def drain(provider, tools=None):
    from api.ai.providers import TextDelta, TurnFinished

    text, finished = "", None
    async for event in provider.stream(
        system="You are an analyst.",
        messages=[{"role": "user", "content": "why did traffic fall?"}],
        tools=tools if tools is not None else [],
    ):
        if isinstance(event, TextDelta):
            text += event.text
        elif isinstance(event, TurnFinished):
            finished = event
    return text, finished


TOOL = {
    "name": "get_movers",
    "description": "what moved",
    "input_schema": {"type": "object", "properties": {"period": {"type": "string"}}},
}


async def test_a_conversation_streams_text_as_it_arrives():
    from api.tests.fake_model import anthropic_chat_over, streamed_message

    provider, requests = anthropic_chat_over(
        responds_stream(streamed_message(text="Your clicks fell by 280."))
    )
    text, finished = await drain(provider)

    assert text == "Your clicks fell by 280."
    assert finished.stop_reason == "end_turn"
    assert finished.tool_calls == []
    assert finished.model == "claude-opus-5"
    assert requests[0]["stream"] is True
    assert requests[0]["thinking"] == {"type": "adaptive"}


async def test_tool_calls_come_back_parsed_with_their_input():
    from api.tests.fake_model import anthropic_chat_over, streamed_message

    provider, requests = anthropic_chat_over(
        responds_stream(
            streamed_message(
                text="Let me look.", tool=("get_movers", {"period": "28d"})
            )
        )
    )
    text, finished = await drain(provider, tools=[TOOL])

    assert text == "Let me look."
    assert finished.stop_reason == "tool_use"
    assert [(c.name, c.input) for c in finished.tool_calls] == [
        ("get_movers", {"period": "28d"})
    ]
    assert requests[0]["tools"][0]["name"] == "get_movers"


async def test_the_last_round_sends_no_tools_field_at_all():
    """Not an empty list — absent. A model handed `tools: []` on the round
    that must produce an answer is being asked an ambiguous question."""
    from api.tests.fake_model import anthropic_chat_over, streamed_message

    provider, requests = anthropic_chat_over(
        responds_stream(streamed_message(text="Done."))
    )
    await drain(provider, tools=[])
    assert "tools" not in requests[0]


async def test_a_streamed_turn_carries_its_usage_and_cost():
    from api.tests.fake_model import anthropic_chat_over, streamed_message

    provider, _ = anthropic_chat_over(
        responds_stream(
            streamed_message(text="ok", input_tokens=10_000, output_tokens=2_000)
        )
    )
    _, finished = await drain(provider)

    assert (finished.input_tokens, finished.output_tokens) == (10_000, 2_000)
    assert str(finished.cost_usd) == "0.100000"


async def test_a_streamed_refusal_is_raised_not_returned():
    from api.tests.fake_model import anthropic_chat_over, streamed_message

    provider, _ = anthropic_chat_over(
        responds_stream(streamed_message(text="", stop_reason="refusal"))
    )
    with pytest.raises(ProviderRefused):
        await drain(provider)


async def test_a_broken_stream_becomes_a_provider_error():
    from api.tests.fake_model import anthropic_chat_over

    provider, _ = anthropic_chat_over(
        responds({"type": "error", "error": {"message": "overloaded"}}, status=529)
    )
    with pytest.raises(ProviderError):
        await drain(provider)
