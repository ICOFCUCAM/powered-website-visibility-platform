"""Checking that a custom domain actually points at this host.

Verification exists to protect the certificate authority's rate limit, not to
prove ownership. Traefik asks for a certificate the moment a hostname appears
in its configuration, and a hostname whose DNS points somewhere else fails the
HTTP-01 challenge. Let's Encrypt counts those failures — five per account per
hostname per hour — so a mistyped domain left in the config can stop every
site on the host from renewing.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VerificationResult:
    verified: bool
    detail: str
    domain_addresses: tuple[str, ...] = ()
    expected_addresses: tuple[str, ...] = ()


async def verify(host: str, *, expected_host: str) -> VerificationResult:
    """Compare where `host` resolves with where the platform's own wildcard
    resolves.

    Comparing against the wildcard rather than a configured IP means there is
    one fact to keep correct instead of two. The wildcard already has to be
    right for any deployment URL to work, so if it is wrong the operator knows
    long before a custom domain is involved.
    """
    domain_addresses, expected_addresses = await asyncio.gather(
        _resolve(host), _resolve(expected_host)
    )

    if not expected_addresses:
        return VerificationResult(
            verified=False,
            detail=(
                f"The platform's own domain {expected_host} does not resolve, "
                "so there is nothing to compare against. Fix the wildcard DNS "
                "record first."
            ),
        )
    if not domain_addresses:
        return VerificationResult(
            verified=False,
            detail=(
                f"{host} does not resolve. Add a CNAME to {expected_host}, or "
                f"an A record to {', '.join(expected_addresses)}."
            ),
            expected_addresses=expected_addresses,
        )

    if set(domain_addresses) & set(expected_addresses):
        return VerificationResult(
            verified=True,
            detail=f"{host} resolves here ({', '.join(domain_addresses)})",
            domain_addresses=domain_addresses,
            expected_addresses=expected_addresses,
        )

    return VerificationResult(
        verified=False,
        detail=(
            f"{host} resolves to {', '.join(domain_addresses)}, but this "
            f"platform is at {', '.join(expected_addresses)}. Point it here "
            "and try again — DNS changes can take a while to propagate."
        ),
        domain_addresses=domain_addresses,
        expected_addresses=expected_addresses,
    )


async def _resolve(host: str) -> tuple[str, ...]:
    """Resolve to every A/AAAA address, off the event loop.

    `getaddrinfo` blocks, and on a failing lookup it can block for seconds.
    Running it inline would stall every other request in the process.
    """
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM
        )
    except socket.gaierror:
        return ()
    return tuple(sorted({info[4][0] for info in infos}))
