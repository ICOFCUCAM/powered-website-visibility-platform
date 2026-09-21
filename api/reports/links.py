"""Signed links to a report's HTML.

A weekly email has to contain a link that works in a mail client, where there
is no session and no Authorization header. The alternatives are a login wall
on every link — which turns a 30-second read into a password reset — or an
unguessable URL.

So: HMAC over the report id and an expiry, with a context string in the
message so a signature minted here can never be replayed against a different
signing use of the same secret.

This is deliberately NOT a session. It grants read access to one rendered
report until it expires, and nothing else.
"""

from __future__ import annotations

import hmac
import time
from hashlib import sha256
from uuid import UUID

CONTEXT = b"visibility-hub/report-html/v1"
#: A weekly email is read over days, not minutes, and a dead link in it is a
#: support ticket. Long enough to be useful, short enough that a forwarded
#: email stops working before the data is stale.
DEFAULT_TTL_SECONDS = 14 * 24 * 3600


def sign(report_id: UUID, secret: str, *, expires_at: int) -> str:
    message = b"|".join(
        (CONTEXT, str(report_id).encode(), str(expires_at).encode())
    )
    return hmac.new(secret.encode(), message, sha256).hexdigest()


def issue(
    report_id: UUID, secret: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> tuple[int, str]:
    expires_at = int(time.time()) + ttl_seconds
    return expires_at, sign(report_id, secret, expires_at=expires_at)


def verify(report_id: UUID, secret: str, *, expires_at: int, signature: str) -> bool:
    # Expiry is checked first so an expired link cannot be probed for
    # signature timing, and compare_digest because a string == here would
    # leak the signature a character at a time.
    if expires_at < int(time.time()):
        return False
    return hmac.compare_digest(
        sign(report_id, secret, expires_at=expires_at), signature
    )
