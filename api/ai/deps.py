"""Choosing a provider.

There may be no provider, and that is a first-class state rather than a
misconfiguration: every caller in this package has a template path, so an
installation with no API key produces plain, correct, hand-written advice and
a plan whose figures are entirely real.

Built once and reused. A client per request would open a connection pool per
request.
"""

from __future__ import annotations

import logging
import os

from api.ai.providers import AnthropicProvider, LLMProvider

logger = logging.getLogger("visibility_hub.ai")

_provider: LLMProvider | None = None
_resolved = False


def set_provider(provider: LLMProvider | None) -> None:
    """Injection point for tests and for a worker that builds its own."""
    global _provider, _resolved
    _provider, _resolved = provider, True


def get_provider() -> LLMProvider | None:
    global _provider, _resolved
    if _resolved:
        return _provider

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.info(
            "no ANTHROPIC_API_KEY: explanations and plans will be templated"
        )
        _provider = None
    else:
        _provider = AnthropicProvider.from_api_key(api_key)
    _resolved = True
    return _provider
