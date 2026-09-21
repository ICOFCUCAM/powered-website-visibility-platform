"""Refusing to fetch things that are not the public web.

The crawler has always been gated on `ownership_verified_at`: it visits a
site only after the customer proved they own it. That gate is the reason no
URL guard was needed. Anything that fetches a URL WITHOUT that gate — a
public scan, a webhook, a preview — has no such protection, and a server that
will fetch any URL on request is a server that will read its own cloud
metadata endpoint and hand the credentials to whoever asked.

So: a URL is safe only if it is http(s), on a normal web port, names a host
that is not an internal name, and resolves ENTIRELY to globally routable
addresses. `is_global` is the single rule that matters — it is false for
private ranges, loopback, link-local (which is where 169.254.169.254 lives),
carrier-grade NAT, benchmark and reserved space, and multicast.

DNS can lie twice: once at resolution and again between the check and the
connection. `check_url` closes the first; `check_peer` closes the second by
looking at the address actually connected to, before any body is read. Both
are needed — either alone is a vulnerability with a name.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

#: Nothing else is the public web. `file:`, `gopher:` and `ftp:` are the
#: classic escapes, and a redirect is allowed to name any of them.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: A site worth scanning is served on a web port. Anything else is somebody
#: pointing us at a database.
ALLOWED_PORTS = frozenset({80, 443})

#: Suffixes that only ever name something inside a network.
INTERNAL_SUFFIXES = (
    ".local", ".localhost", ".internal", ".intranet", ".lan",
    ".home.arpa", ".corp", ".private", ".test", ".example", ".invalid",
)


class UnsafeUrl(ValueError):
    """The URL is not somewhere we are willing to send a request."""


def _address_is_public(raw: str) -> bool:
    """True only for a globally routable address.

    IPv4-mapped and 6to4 addresses are unwrapped first: `::ffff:127.0.0.1` is
    loopback wearing a hat, and a check that does not unwrap it lets loopback
    straight through.
    """
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour

    return ip.is_global


def check_url(url: str) -> str:
    """Return the URL if it is safe to request, or raise `UnsafeUrl`.

    Resolves the hostname and requires EVERY address it resolves to be
    public: a name with one public and one private answer is a round-robin
    away from being an internal fetch.
    """
    parts = urlsplit(url)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrl(f"{parts.scheme or 'that'} is not a web address")

    # user:password@host is how a crafted URL smuggles credentials, and how
    # a parser disagreement turns "evil.com@internal" into "internal".
    if parts.username or parts.password:
        raise UnsafeUrl("a web address does not carry a username or password")

    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise UnsafeUrl("that address has no host")

    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:  # non-numeric port
        raise UnsafeUrl("that address has no valid port") from exc
    if port not in ALLOWED_PORTS:
        raise UnsafeUrl("only ports 80 and 443 are scanned")

    if host == "localhost" or host.endswith(INTERNAL_SUFFIXES):
        raise UnsafeUrl("that host is not on the public internet")
    # A name with no dot resolves through the machine's own search domains.
    if "." not in host and not _looks_like_ip(host):
        raise UnsafeUrl("that host is not on the public internet")

    # An IP literal never needs resolving, and must pass on its own.
    if _looks_like_ip(host):
        if not _address_is_public(host):
            raise UnsafeUrl("that address is not on the public internet")
        return url

    for address in _resolve(host, port):
        if not _address_is_public(address):
            raise UnsafeUrl("that host resolves to a private address")
    return url


def check_peer(address: str | None) -> None:
    """Refuse a connection that landed somewhere private.

    Called once the socket is open and the headers are in, BEFORE the body is
    read. This is what closes the window between `check_url` resolving a name
    and the connection resolving it again — the rebinding attack that every
    allowlist built on DNS alone is vulnerable to.
    """
    if address is None:
        # No peer information from the transport. Fail closed: this runs on
        # behalf of an unauthenticated stranger.
        raise UnsafeUrl("could not confirm where that connected")
    if not _address_is_public(address):
        raise UnsafeUrl("that host resolves to a private address")


def _looks_like_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrl("that host does not resolve") from exc
    if not infos:
        raise UnsafeUrl("that host does not resolve")
    return [info[4][0] for info in infos]
