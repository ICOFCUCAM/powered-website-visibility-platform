"""Access-token lifecycle.

Access tokens are short-lived and never persisted: only their expiry is stored,
so a worker knows whether to refresh before calling Google. Refresh tokens live
sealed in the vault and are read for the length of one refresh.

The important behaviour is what happens when a refresh fails. Google returns
`invalid_grant` when the user revoked access, changed their password, or the
token went unused for six months. That is terminal: the connection is marked
`needs_reauth`, scheduled work stops, and the UI shows a reconnect card.
Retrying a dead grant is how an OAuth client earns a rate limit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection

from api.hub.providers.google.client import GoogleClient, GoogleTokens
from api.hub.providers.google.errors import GoogleRefreshRejected
from api.hub.repositories import HubRepository
from api.hub.services.vault import TokenVault

#: Refresh this far before expiry rather than at it, so a long request cannot
#: start with a valid token and finish with an expired one.
EXPIRY_SKEW_SECONDS = 120


@dataclass(slots=True)
class CachedToken:
    access_token: str
    expires_at: float

    @property
    def usable(self) -> bool:
        return time.time() < self.expires_at - EXPIRY_SKEW_SECONDS


class TokenService:
    """Per-request cache of access tokens, keyed by connection.

    Deliberately not a process-wide cache: an access token is a credential, and
    a long-lived shared cache is a larger blast radius than one extra refresh
    call is worth.
    """

    def __init__(
        self,
        conn: AsyncConnection,
        vault: TokenVault,
        client: GoogleClient,
    ) -> None:
        self._conn = conn
        self._vault = vault
        self._client = client
        self._repo = HubRepository(conn)
        self._cache: dict[UUID, CachedToken] = {}

    async def access_token_for(self, connection_id: UUID) -> str:
        cached = self._cache.get(connection_id)
        if cached and cached.usable:
            return cached.access_token

        connection = await self._repo.get_connection(connection_id)
        if connection is None:
            raise GoogleRefreshRejected("That Google connection no longer exists.")
        if connection["status"] == "revoked":
            raise GoogleRefreshRejected()
        if not connection["refresh_token_id"]:
            raise GoogleRefreshRejected()

        refresh_token = await self._vault.read(connection["refresh_token_id"])
        if refresh_token is None:
            await self._repo.mark_needs_reauth(connection_id, "refresh_token_missing")
            raise GoogleRefreshRejected()

        try:
            tokens = await self._client.refresh(refresh_token)
        except GoogleRefreshRejected:
            await self._repo.mark_needs_reauth(connection_id, "invalid_grant")
            raise

        # Google sometimes issues a replacement refresh token. When it does the
        # old one stops working, so keeping it would guarantee a later failure.
        if tokens.refresh_token and tokens.refresh_token != refresh_token:
            await self._vault.replace(
                connection["refresh_token_id"], tokens.refresh_token
            )

        await self._conn.execute(
            "update connections set last_refreshed_at = now(), "
            "       access_token_expires_at = now() + make_interval(secs => %s), "
            "       status = 'active', last_error = null "
            " where id = %s",
            (tokens.expires_in, connection_id),
        )

        self._cache[connection_id] = CachedToken(
            access_token=tokens.access_token,
            expires_at=time.time() + tokens.expires_in,
        )
        return tokens.access_token

    def remember(self, connection_id: UUID, tokens: GoogleTokens) -> None:
        """Seed the cache from a just-completed authorization, so the discovery
        that immediately follows does not spend a refresh call."""
        self._cache[connection_id] = CachedToken(
            access_token=tokens.access_token,
            expires_at=time.time() + tokens.expires_in,
        )
