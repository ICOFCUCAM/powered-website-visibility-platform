"""Browser sessions for the dashboard.

The API authenticates with a bearer token, which a browser cannot send on a
plain navigation. Rather than introduce a second credential, signing in means
presenting that same token once and receiving a signed cookie for it.

There is no session table. The cookie carries its own issue time and a
signature over it, so the server needs to remember nothing — which also means
there is nothing to clean up, and a restart does not sign everyone out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

COOKIE_NAME = "forge_session"

#: A week. Long enough not to be a nuisance on a tool checked a few times a
#: day, short enough that a cookie copied off a machine stops working.
MAX_AGE_SECONDS = 7 * 24 * 3600


class InvalidSession(Exception):
    pass


def issue(*, token: str, now: float | None = None) -> str:
    """Mint a cookie value proving the holder presented the API token.

    The signing key is derived from the token rather than being the token, so
    the cookie is not itself a bearer credential for the API: stealing it
    gets the dashboard, not the API, and rotating the token invalidates every
    outstanding cookie for free.
    """
    issued = int(now if now is not None else time.time())
    payload = str(issued).encode()
    signature = _sign(payload, token)
    return f"{_b64(payload)}.{_b64(signature)}"


def verify(cookie: str, *, token: str, now: float | None = None) -> int:
    """Return the issue time, or raise. Never returns for an invalid cookie."""
    try:
        encoded_payload, encoded_signature = cookie.split(".", 1)
        payload = _unb64(encoded_payload)
        signature = _unb64(encoded_signature)
    except (ValueError, TypeError) as exc:
        raise InvalidSession("malformed session cookie") from exc

    if not hmac.compare_digest(signature, _sign(payload, token)):
        raise InvalidSession("session signature does not match")

    try:
        issued = int(payload.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidSession("malformed session payload") from exc

    current = now if now is not None else time.time()
    if current - issued > MAX_AGE_SECONDS:
        raise InvalidSession("session has expired")
    # A cookie issued in the future is either a clock that moved backwards or
    # a forgery attempt; neither is a session worth honouring.
    if issued - current > 60:
        raise InvalidSession("session was issued in the future")
    return issued


def _sign(payload: bytes, token: str) -> bytes:
    key = hashlib.sha256(f"forge.session.v1:{token}".encode()).digest()
    return hmac.new(key, payload, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
