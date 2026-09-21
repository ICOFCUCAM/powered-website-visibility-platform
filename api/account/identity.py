"""Deleting the login, not just the data.

An account deletion that leaves the sign-in working has not deleted the
account. The application stores no credentials of its own (migration 0001:
there is no `password_hash` column, because a table that cannot leak one is
strictly safer than a table that can), so the identity lives with the auth
provider and has to be deleted through it.

Same shape as the mailer and the model provider: a protocol, a real
implementation, and a recorded absence. An installation with no admin key
configured deletes every byte of application data and SAYS the login remains —
which is a worse outcome than deleting both, and a much better one than
claiming to have done it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx

logger = logging.getLogger("visibility_hub.account")

TIMEOUT = httpx.Timeout(10.0)


@dataclass(frozen=True, slots=True)
class IdentityOutcome:
    deleted: bool
    #: Set when it was not. Reported to the customer rather than swallowed:
    #: "your data is gone but your sign-in still works" is something they need
    #: to be told, not something to discover.
    reason: str | None = None


class IdentityProvider(Protocol):
    async def delete(self, user_id: UUID) -> IdentityOutcome: ...


class NoIdentityProvider:
    """No admin credentials configured. Says so rather than pretending."""

    async def delete(self, user_id: UUID) -> IdentityOutcome:
        return IdentityOutcome(
            deleted=False, reason="no identity provider is configured"
        )


class SupabaseIdentityProvider:
    """Supabase Auth's admin API.

    The service-role key is an all-powerful credential and lives only in the
    environment of the process that needs it — never in the database, never in
    a request, never in a log.
    """

    def __init__(
        self, url: str, service_key: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._url = url.rstrip("/")
        self._key = service_key
        self._client = client

    @classmethod
    def from_env(cls) -> IdentityProvider:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            return NoIdentityProvider()
        return cls(url, key)

    async def delete(self, user_id: UUID) -> IdentityOutcome:
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            response = await client.delete(
                f"{self._url}/auth/v1/admin/users/{user_id}",
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "apikey": self._key,
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("could not reach the identity provider: %s", exc)
            return IdentityOutcome(
                deleted=False, reason=f"{type(exc).__name__}"
            )
        finally:
            if self._client is None:
                await client.aclose()

        # 404 means the identity is already gone, which is the outcome we
        # wanted. Anything else is a failure worth naming.
        if response.status_code in (200, 204, 404):
            return IdentityOutcome(deleted=True)
        logger.warning(
            "identity provider refused the deletion: status=%s", response.status_code
        )
        return IdentityOutcome(
            deleted=False, reason=f"the identity provider returned {response.status_code}"
        )
