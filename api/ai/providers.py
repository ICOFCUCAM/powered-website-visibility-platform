"""Talking to a language model.

The only module in the codebase allowed to import the Anthropic SDK, enforced
by an import contract in pyproject.toml. Everything else asks for a
`Generation` and gets structured data back, which is what makes the model
swappable and the whole layer testable against a transport rather than a
network.

Two tiers, from docs/06-ai-layer.md: bulk explanations run on the cheap model
because they are high volume and low reasoning; the weekly plan runs on the
frontier model because it synthesises ranked findings against last week's
history. That split is the spec's, not a cost decision taken here.

Failure is ordinary, not exceptional. Every caller has a template fallback, so
this module raises a typed error and lets the caller decide — it never retries
into a bill and never returns half a generation.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

from api.ai.pricing import cost_usd

logger = logging.getLogger("visibility_hub.ai")

Tier = Literal["cheap", "frontier"]

#: Non-streaming is correct here and worth stating: every prompt in this layer
#: produces a small structured object — a four-item plan, a three-field
#: explanation — well under the ~16k output where a non-streamed request starts
#: risking an HTTP timeout. The Strategist (M9) is interactive and will stream.
MAX_OUTPUT_TOKENS = 4096


class ProviderError(RuntimeError):
    """The model did not return usable output. The caller falls back."""


class ProviderRefused(ProviderError):
    """The model declined to answer. Never retried, never papered over."""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    model: str
    #: Omitted entirely for the cheap tier: bulk explanation is a formatting
    #: task over evidence that has already been reasoned about by the rules
    #: engine, and thinking tokens on it are spend without a product.
    thinking: dict[str, Any] | None


TIERS: dict[Tier, ModelSpec] = {
    "cheap": ModelSpec(model="claude-haiku-4-5-20251001", thinking=None),
    "frontier": ModelSpec(model="claude-opus-5", thinking={"type": "adaptive"}),
}


@dataclass(frozen=True, slots=True)
class Generation:
    """One model call: what it produced and what it cost."""

    data: dict[str, Any]
    model: str
    model_provider: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    latency_ms: int
    cost_usd: Decimal | None

    @property
    def structured(self) -> bool:
        return isinstance(self.data, dict)


class LLMProvider(Protocol):
    async def generate(
        self,
        *,
        tier: Tier,
        system: str,
        prompt: str,
        schema: dict[str, Any],
    ) -> Generation: ...


def _extract_json(content: list[Any]) -> dict[str, Any]:
    """The first text block, parsed.

    Thinking blocks come first on the frontier tier, so the text block is
    found by type rather than by position.
    """
    for block in content:
        if getattr(block, "type", None) == "text":
            text = block.text.strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProviderError(f"model returned unparseable JSON: {exc}") from exc
    raise ProviderError("model returned no text block")


class AnthropicProvider:
    """The real provider.

    Takes an already-built SDK client so the tests can hand it a transport.
    Nothing here knows about HTTP.
    """

    name = "anthropic"

    def __init__(self, client: Any, *, tiers: dict[Tier, ModelSpec] | None = None):
        self._client = client
        self._tiers = tiers or TIERS

    @classmethod
    def from_api_key(cls, api_key: str, **kwargs: Any) -> AnthropicProvider:
        from anthropic import AsyncAnthropic

        return cls(AsyncAnthropic(api_key=api_key), **kwargs)

    async def generate(
        self,
        *,
        tier: Tier,
        system: str,
        prompt: str,
        schema: dict[str, Any],
    ) -> Generation:
        spec = self._tiers[tier]
        request: dict[str, Any] = {
            "model": spec.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            # Structured output rather than "reply with only JSON" and a
            # retry-on-parse loop: the schema is enforced server-side, and the
            # prompt stays about the content instead of the format.
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        if spec.thinking is not None:
            request["thinking"] = spec.thinking

        started = time.monotonic()
        try:
            message = await self._call(request)
        except ProviderError:
            raise
        except Exception as exc:  # SDK errors: connection, rate limit, 5xx
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        # Checked before the content is read, because on a refusal the content
        # is not an answer and must not be parsed as one.
        if message.stop_reason == "refusal":
            raise ProviderRefused(f"model refused: {message.stop_details}")
        if message.stop_reason == "max_tokens":
            raise ProviderError("generation was truncated at max_tokens")

        usage = message.usage
        cached = int(getattr(usage, "cache_read_input_tokens", None) or 0)
        input_tokens = int(usage.input_tokens or 0) + cached
        output_tokens = int(usage.output_tokens or 0)

        return Generation(
            data=_extract_json(message.content),
            model=message.model or spec.model,
            model_provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
            latency_ms=latency_ms,
            cost_usd=cost_usd(
                message.model or spec.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached,
            ),
        )

    async def _call(self, request: dict[str, Any]) -> Any:
        """One retry, and only for the one thing worth retrying.

        A model that does not accept `output_config` is a configuration fact,
        not a transient failure: retrying without it gets the same content
        through the text block, and our own validation runs either way. Every
        other error goes straight to the caller's template fallback rather
        than spending a second call to fail again.
        """
        try:
            return await self._client.messages.create(**request)
        except Exception as exc:
            if not _is_unsupported_output_config(exc):
                raise
            logger.info(
                "model does not support structured output, retrying without: "
                "model=%s",
                request["model"],
            )
            retry = {k: v for k, v in request.items() if k != "output_config"}
            retry["system"] = (
                request["system"]
                + "\n\nReturn a single JSON object and nothing else. It must "
                "match this schema exactly:\n"
                + json.dumps(request["output_config"]["format"]["schema"])
            )
            return await self._client.messages.create(**retry)


def _is_unsupported_output_config(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status != 400:
        return False
    return "output_config" in str(exc) or "json_schema" in str(exc)
