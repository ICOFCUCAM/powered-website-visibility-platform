"""Key management for the token vault.

Envelope encryption: each secret gets its own data key (DEK), and the DEK is
wrapped by a master key that lives outside the database. Compromising the
database yields ciphertext and wrapped DEKs, neither of which is useful without
the master key; rotating the master key rewraps DEKs without re-encrypting
every secret.

The master key never appears in a migration, a log line or a response.
"""

from __future__ import annotations

import base64
import os
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from api.config import ConfigError

WRAP_NONCE_BYTES = 12


class KeyManager(Protocol):
    """Wraps and unwraps data keys."""

    @property
    def key_version(self) -> int: ...

    def wrap(self, dek: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes) -> bytes: ...


class LocalKeyManager:
    """Master key from the environment.

    Adequate for development and for a single-tenant deployment where the key
    is injected by the platform's own secret store. Production on a cloud
    provider should use that provider's KMS instead — the point of this
    Protocol is that swapping it touches nothing else.
    """

    def __init__(self, master_key: bytes, key_version: int = 1) -> None:
        if len(master_key) != 32:
            raise ConfigError("TOKEN_MASTER_KEY must decode to exactly 32 bytes")
        self._aead = AESGCM(master_key)
        self._key_version = key_version

    @classmethod
    def from_base64(cls, encoded: str, key_version: int = 1) -> LocalKeyManager:
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001 - any decode failure is fatal
            raise ConfigError("TOKEN_MASTER_KEY must be valid base64") from exc
        return cls(raw, key_version)

    @property
    def key_version(self) -> int:
        return self._key_version

    def wrap(self, dek: bytes) -> bytes:
        nonce = os.urandom(WRAP_NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, dek, b"dek")

    def unwrap(self, wrapped: bytes) -> bytes:
        nonce, ciphertext = wrapped[:WRAP_NONCE_BYTES], wrapped[WRAP_NONCE_BYTES:]
        return self._aead.decrypt(nonce, ciphertext, b"dek")


def generate_master_key() -> str:
    """For `python -c` during setup. Never call this at runtime."""
    return base64.b64encode(os.urandom(32)).decode()
