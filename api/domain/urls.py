"""URL normalisation for website onboarding (V1 spec s13, step 1).

The user types whatever they think their website is called. The platform's
premise is that they should never have to understand the difference between
`example.com`, `https://www.example.com/` and a domain property, so this module
absorbs it.

Pure functions, no network. DNS and HTTP validation (s13 steps 2-3) happen in
the service layer; this decides only what the input *means*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from api.domain.errors import InvalidWebsite

# Deliberately permissive on TLD length (new gTLDs are long) and strict on the
# label shape: no leading/trailing hyphen, no empty labels, no underscores.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")

_DEFAULT_PORTS = {"http": "80", "https": "443"}

# Hosts that are never a customer website. Not a security boundary on its own —
# the crawler re-resolves and re-checks — but it catches the obvious mistake
# before a row is created.
_RESERVED_HOSTS = frozenset(
    {"localhost", "localhost.localdomain", "broadcasthost", "ip6-localhost"}
)


@dataclass(frozen=True, slots=True)
class NormalisedWebsite:
    """What the user meant.

    `domain` is the registrable-ish host without `www.`, used for dedupe and
    for matching Search Console properties. `canonical_url` is the origin we
    will actually fetch, preserving the `www.` they typed, because
    `www.example.com` and `example.com` can serve different sites.
    """

    domain: str
    canonical_url: str
    host: str
    scheme: str

    @property
    def had_www(self) -> bool:
        return self.host.startswith("www.")


def normalise_website_input(raw: str) -> NormalisedWebsite:
    """Turn anything a human types into a domain and a canonical origin.

    Accepts `example.com`, `www.example.com`, `HTTP://Example.com/path?a=1`,
    `https://example.com:443/`. Rejects IP addresses, reserved hosts, userinfo,
    and anything that is not a plausible hostname.
    """
    if raw is None:
        raise InvalidWebsite()

    value = raw.strip()
    if not value:
        raise InvalidWebsite()

    # Reject control characters and whitespace outright rather than stripping
    # them: they are far more likely to be a paste error than an intention.
    if any(c.isspace() or ord(c) < 0x20 for c in value):
        raise InvalidWebsite("Website addresses cannot contain spaces.")

    scheme, _, rest = value.partition("://")
    if not rest:
        scheme, rest = "https", value
    else:
        scheme = scheme.lower()
        if scheme not in ("http", "https"):
            raise InvalidWebsite("Only http and https websites are supported.")

    # Strip path, query and fragment — we only ever store an origin.
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if not authority:
        raise InvalidWebsite()

    # Credentials in a URL are never something we want to store or fetch.
    if "@" in authority:
        raise InvalidWebsite("Remove the username from the address.")

    host, _, port = authority.partition(":")
    host = host.rstrip(".").lower()

    if port:
        if not port.isdigit():
            raise InvalidWebsite()
        # A default port is noise; a non-default one is almost never a real
        # public website and would silently split dedupe.
        if port != _DEFAULT_PORTS[scheme]:
            raise InvalidWebsite("Websites on a custom port aren't supported.")

    if not host or host in _RESERVED_HOSTS:
        raise InvalidWebsite()

    # An IP literal is not a website we can verify ownership of.
    if re.fullmatch(r"[0-9.]+", host) or host.startswith("["):
        raise InvalidWebsite("Enter a domain name rather than an IP address.")

    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidWebsite() from exc

    if not _HOSTNAME.fullmatch(host):
        raise InvalidWebsite()

    domain = host[4:] if host.startswith("www.") else host

    return NormalisedWebsite(
        domain=domain,
        canonical_url=f"{scheme}://{host}",
        host=host,
        scheme=scheme,
    )
