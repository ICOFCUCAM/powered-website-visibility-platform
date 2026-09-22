"""Encryption for stored environment variables.

Fernet: AES-128-CBC with an HMAC, authenticated, with the key supplied by the
operator rather than derived from anything guessable. The threat being
defended against is specific and ordinary — a database backup copied somewhere
less careful than the database — and the property that matters is that the
backup alone is not enough.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from forge.config import ConfigError


class DecryptionFailed(RuntimeError):
    """The ciphertext did not verify under the current master key.

    Almost always means the key was rotated without re-encrypting. Raised
    loudly rather than returning an empty string, because an application that
    starts with a silently blank DATABASE_URL fails much further from the
    cause.
    """


@lru_cache(maxsize=4)
def _cipher(key: str) -> Fernet:
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise ConfigError(
            "FORGE_MASTER_KEY is not a valid Fernet key. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc


def encrypt(plaintext: str, *, key: str) -> bytes:
    return _cipher(key).encrypt(plaintext.encode())


def decrypt(ciphertext: bytes, *, key: str) -> str:
    try:
        return _cipher(key).decrypt(bytes(ciphertext)).decode()
    except InvalidToken as exc:
        raise DecryptionFailed(
            "An environment variable could not be decrypted with the current "
            "FORGE_MASTER_KEY."
        ) from exc
