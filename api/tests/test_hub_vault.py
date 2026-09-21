"""The token vault.

A refresh token is long-lived access to a customer's Google account. These
tests check the properties that matter if the database is ever copied.
"""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from api.adapters import db
from api.config import ConfigError
from api.hub.services.keys import LocalKeyManager, generate_master_key
from api.hub.services.vault import TokenVault, seal, unseal

TOKEN = "1//refresh-token-that-must-never-leak"


def _keys() -> LocalKeyManager:
    return LocalKeyManager.from_base64(generate_master_key())


def test_a_sealed_secret_does_not_contain_the_plaintext():
    sealed = seal(TOKEN, _keys())
    assert TOKEN.encode() not in sealed.ciphertext
    assert TOKEN.encode() not in sealed.wrapped_dek


def test_round_trips_with_the_same_master_key():
    keys = _keys()
    assert unseal(seal(TOKEN, keys), keys) == TOKEN


def test_a_different_master_key_cannot_read_it():
    """The database alone is not enough. This is the entire point of wrapping
    the data key with a master key held outside it."""
    sealed = seal(TOKEN, _keys())
    with pytest.raises(InvalidTag):
        unseal(sealed, _keys())


def test_every_secret_gets_its_own_data_key():
    keys = _keys()
    a, b = seal(TOKEN, keys), seal(TOKEN, keys)
    assert a.wrapped_dek != b.wrapped_dek
    assert a.ciphertext != b.ciphertext  # and its own nonce


def test_tampering_with_the_ciphertext_is_detected():
    """AES-GCM is authenticated: a modified row fails loudly rather than
    decrypting to something else."""
    keys = _keys()
    sealed = seal(TOKEN, keys)
    flipped = bytes([sealed.ciphertext[0] ^ 0x01]) + sealed.ciphertext[1:]
    with pytest.raises(InvalidTag):
        unseal(type(sealed)(flipped, sealed.wrapped_dek, sealed.nonce, 1), keys)


def test_a_short_master_key_is_refused():
    with pytest.raises(ConfigError):
        LocalKeyManager(b"too-short")


async def test_stores_and_reads_through_the_service_connection(client):
    if not os.environ.get("SERVICE_DATABASE_URL"):
        pytest.skip("SERVICE_DATABASE_URL is not configured")

    keys = _keys()
    async with db.service_session() as conn:
        vault = TokenVault(conn, keys)
        token_id = await vault.store(TOKEN)
        assert await vault.read(token_id) == TOKEN

        # What is actually on disk is ciphertext.
        row = await (
            await conn.execute(
                "select ciphertext from secrets.oauth_tokens where id = %s",
                (token_id,),
            )
        ).fetchone()
        assert TOKEN.encode() not in bytes(row["ciphertext"])

        await vault.replace(token_id, "1//rotated")
        assert await vault.read(token_id) == "1//rotated"

        await vault.destroy(token_id)
        assert await vault.read(token_id) is None
