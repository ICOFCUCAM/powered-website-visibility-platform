"""Internal domain events.

These are NOT Google webhooks. Google does not push Search Console or
Analytics reporting data to this application; every figure arrives because a
scheduled job went and asked for it. These events are how the Hub tells the
core that new facts are queryable, without the core importing the Hub.

In-process for now. The publisher is a seam: when the Hub becomes its own
service, `publish` posts to a queue and nothing else changes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("visibility_hub.events")

SYNC_COMPLETED = "hub.sync.completed"
SYNC_FAILED = "hub.sync.failed"
CONNECTION_REVOKED = "hub.connection.revoked"
PROPERTY_CONNECTED = "hub.property.connected"


@dataclass(frozen=True, slots=True)
class DomainEvent:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


Handler = Callable[[DomainEvent], Awaitable[None]]

_handlers: dict[str, list[Handler]] = {}


def subscribe(event_name: str, handler: Handler) -> None:
    _handlers.setdefault(event_name, []).append(handler)


async def publish(event: DomainEvent) -> None:
    logger.info("event=%s", event.name)
    for handler in _handlers.get(event.name, []):
        try:
            await handler(event)
        except Exception:
            # A subscriber must never break the publisher: a failed core
            # reaction cannot roll back a sync that genuinely landed.
            logger.exception("handler failed for %s", event.name)


def reset() -> None:
    """Tests only."""
    _handlers.clear()
