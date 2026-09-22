"""Environment variable encryption."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from forge.adapters.crypto import DecryptionFailed, decrypt, encrypt
from forge.config import ConfigError


def test_a_value_survives_a_round_trip():
    key = Fernet.generate_key().decode()
    assert decrypt(encrypt("s3cret", key=key), key=key) == "s3cret"


def test_the_ciphertext_does_not_contain_the_plaintext():
    key = Fernet.generate_key().decode()
    assert b"s3cret" not in encrypt("s3cret", key=key)


def test_a_different_key_cannot_read_it():
    """This is the property that makes a stolen database backup useless on
    its own."""
    written = encrypt("s3cret", key=Fernet.generate_key().decode())
    with pytest.raises(DecryptionFailed):
        decrypt(written, key=Fernet.generate_key().decode())


def test_a_rotated_key_fails_loudly_rather_than_returning_nothing():
    """An app that starts with a silently blank DATABASE_URL fails a long way
    from the cause."""
    written = encrypt("postgres://…", key=Fernet.generate_key().decode())
    with pytest.raises(DecryptionFailed) as exc:
        decrypt(written, key=Fernet.generate_key().decode())
    assert "FORGE_MASTER_KEY" in str(exc.value)


def test_a_malformed_key_explains_how_to_make_a_good_one():
    with pytest.raises(ConfigError) as exc:
        encrypt("x", key="not-a-fernet-key")
    assert "generate_key" in str(exc.value)
