"""The token vault.

Refresh tokens are the crown jewels: one is long-lived access to a customer's
Google account. They live in the `secrets` schema, which has no RLS policies
and no grants, so no client role can read it under any circumstance — and even
the backend only ever holds ciphertext until the moment it needs the token.

Nothing here returns a plaintext token to a caller that has not asked for it
explicitly, and no function in this module logs one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import AsyncConnection

from api.adapters.db import fetch_one
from api.hub.services.keys import KeyManager

ALGORITHM = "AES-256-GCM"
NONCE_BYTES = 12
_ASSOCIATED_DATA = b"oauth-refresh-token"


@dataclass(frozen=True, slots=True)
class SealedSecret:
    ciphertext: bytes
    wrapped_dek: bytes
    nonce: bytes
    key_version: int
    algo: str = ALGORITHM


def seal(plaintext: str, keys: KeyManager) -> SealedSecret:
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(
        nonce, plaintext.encode("utf-8"), _ASSOCIATED_DATA
    )
    return SealedSecret(
        ciphertext=ciphertext,
        wrapped_dek=keys.wrap(dek),
        nonce=nonce,
        key_version=keys.key_version,
    )


def unseal(sealed: SealedSecret, keys: KeyManager) -> str:
    dek = keys.unwrap(sealed.wrapped_dek)
    return AESGCM(dek).decrypt(
        sealed.nonce, sealed.ciphertext, _ASSOCIATED_DATA
    ).decode("utf-8")


class TokenVault:
    """Stores and retrieves sealed secrets.

    Runs on a connection held by the service role. The `secrets` schema is
    unreachable from the client roles the API normally uses, which is the whole
    point of putting it there.
    """

    def __init__(self, conn: AsyncConnection, keys: KeyManager) -> None:
        self._conn = conn
        self._keys = keys

    async def store(self, plaintext: str) -> UUID:
        sealed = seal(plaintext, self._keys)
        row = await fetch_one(
            self._conn,
            """
            insert into secrets.oauth_tokens
                (ciphertext, wrapped_dek, nonce, key_version, algo)
            values (%s, %s, %s, %s, %s)
            returning id
            """,
            (
                sealed.ciphertext,
                sealed.wrapped_dek,
                sealed.nonce,
                sealed.key_version,
                sealed.algo,
            ),
        )
        assert row is not None
        return row["id"]

    async def read(self, token_id: UUID) -> str | None:
        row = await fetch_one(
            self._conn,
            "select ciphertext, wrapped_dek, nonce, key_version, algo "
            "from secrets.oauth_tokens where id = %s",
            (token_id,),
        )
        if row is None:
            return None
        return unseal(
            SealedSecret(
                ciphertext=bytes(row["ciphertext"]),
                wrapped_dek=bytes(row["wrapped_dek"]),
                nonce=bytes(row["nonce"]),
                key_version=row["key_version"],
                algo=row["algo"],
            ),
            self._keys,
        )

    async def replace(self, token_id: UUID, plaintext: str) -> None:
        """Google may issue a new refresh token on refresh; when it does, the
        old one stops working and keeping it would guarantee a future failure."""
        sealed = seal(plaintext, self._keys)
        await self._conn.execute(
            """
            update secrets.oauth_tokens
               set ciphertext = %s, wrapped_dek = %s, nonce = %s,
                   key_version = %s, rotated_at = now()
             where id = %s
            """,
            (
                sealed.ciphertext,
                sealed.wrapped_dek,
                sealed.nonce,
                sealed.key_version,
                token_id,
            ),
        )

    async def destroy(self, token_id: UUID) -> None:
        """Disconnect must actually disconnect (V1 spec §37)."""
        await self._conn.execute(
            "delete from secrets.oauth_tokens where id = %s", (token_id,)
        )
