"""Issue identity.

    fingerprint = sha256("{type_key}:{scope_type}:{scope_ref}")

Deterministic, so "missing title on /about-us" is the SAME row in week 1 and
week 40. That identity is what makes the lifecycle possible — detected,
applied, verified, regressed — and it is why detection is rule code rather
than a model: a generative detector produces a different list on Tuesday than
on Monday, and nothing can ever be proved fixed.
"""

from __future__ import annotations

import hashlib


def fingerprint(type_key: str, scope_type: str, scope_ref: str | None) -> str:
    return hashlib.sha256(
        f"{type_key}:{scope_type}:{scope_ref or ''}".encode()
    ).hexdigest()


def page_scope(url_hash: bytes) -> str:
    """Scoped by URL hash rather than page id.

    A page row can be recreated — deleted and re-found by a later crawl — and
    its id would change. The URL would not, and the issue is about the URL.
    """
    return url_hash.hex()


def keyword_scope(phrase: str) -> str:
    return hashlib.sha256(phrase.lower().encode()).hexdigest()[:32]


def website_scope() -> None:
    """Website-wide issues have no scope ref: one per website per type."""
    return None
