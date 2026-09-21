"""Giving a customer's Google access back to them.

Owned by the Hub for the same reason `sync_website` is: only the Hub may
construct or call a Google API client, and account deletion needs one. The
caller asks for an organisation's connections to be revoked and gets counts
back — it never sees a token, a client, or a vault.

Best effort, and deliberately so. If Google is unreachable we still destroy
the stored secret, because the customer asked us to delete their data and
keeping their refresh token until Google answers the phone is not an option.
The failure is counted and returned so it ends up on the deletion receipt
rather than in a log nobody reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection

from api.adapters.db import fetch_all
from api.hub import deps
from api.hub.providers.google.client import GoogleClient
from api.hub.services.keys import LocalKeyManager
from api.hub.services.vault import TokenVault

logger = logging.getLogger("visibility_hub.hub")


@dataclass(frozen=True, slots=True)
class Revocation:
    revoked: int = 0
    #: Destroyed locally but not revoked at Google. The token is unusable by
    #: us and will expire on Google's schedule.
    not_revoked: int = 0


async def revoke_organisation(
    conn: AsyncConnection,
    organization_id: UUID,
    *,
    client: GoogleClient | None = None,
    vault: TokenVault | None = None,
    revoke_at_google: bool = True,
) -> Revocation:
    """Revoke and destroy every stored token for one organisation.

    The connection must be the SERVICE role: the vault lives in `secrets`,
    which no other role can reach.

    `revoke_at_google=False` skips the network call and destroys the secrets
    anyway. It exists so a caller with no Google credentials — a test, an
    installation being torn down — still goes through the vault rather than
    reaching into `secrets` itself. Exactly one module knows how to read that
    table, and a deletion path is not a reason to make it two.
    """
    if vault is None or (revoke_at_google and client is None):
        settings = deps.google_settings_from_env()
        if revoke_at_google:
            client = client or deps.google_client_from(settings)
        vault = vault or TokenVault(
            conn, LocalKeyManager.from_base64(settings.token_master_key)
        )

    rows = await fetch_all(
        conn,
        "select id, refresh_token_id from connections"
        " where organization_id = %s and refresh_token_id is not null",
        (organization_id,),
    )

    revoked = failed = 0
    for row in rows:
        token = await vault.read(row["refresh_token_id"]) if revoke_at_google else None
        if token and client is not None:
            try:
                await client.revoke(token)
                revoked += 1
            except Exception as exc:
                # Counted, not raised. The secret still goes.
                logger.warning(
                    "could not revoke a token with Google during deletion: %s", exc
                )
                failed += 1
        # Unconditional: the stored secret is destroyed whether or not Google
        # took the call. A refresh token kept "until we can revoke it" is a
        # refresh token kept.
        await vault.destroy(row["refresh_token_id"])

    return Revocation(revoked=revoked, not_revoked=failed)
