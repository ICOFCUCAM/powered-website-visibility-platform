"""Matching a discovered Google property to a website.

Pure functions, no I/O. This is what turns "here are your 47 Search Console
properties" into "we found yours" — the difference between the wizard feeling
automatic and feeling like configuration.

Note carefully what this does NOT do: it does not decide ownership. Matching
*proposes* a link; ownership is decided by `app.property_covers_url()` plus the
permission level (migration 0010), because a property can match a website by
host while proving nothing about it — a URL-prefix property for
`https://www.example.com/` matches the site but does not cover
`http://www.example.com/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from urllib.parse import urlsplit

DOMAIN_PROPERTY_PREFIX = "sc-domain:"

#: Permission levels that can actually read data. `siteUnverifiedUser` sees the
#: property listed and nothing else.
USABLE_PERMISSIONS = frozenset({"siteOwner", "siteFullUser", "siteRestrictedUser"})
OWNER_PERMISSIONS = frozenset({"siteOwner", "siteFullUser"})


class MatchQuality(IntEnum):
    """Higher is better. Ordering is the product decision, not an accident.

    A domain property outranks a URL-prefix property because it covers every
    subdomain and scheme, so it keeps working when the customer moves from
    `www.` to apex or adds HTTPS.
    """

    NONE = 0
    SUBDOMAIN = 1          # property is for a parent domain of the website
    URL_PREFIX_HOST = 2    # exact host, one scheme only
    DOMAIN_PROPERTY = 3    # every subdomain, every scheme


@dataclass(frozen=True, slots=True)
class PropertyMatch:
    property_uri: str
    matched_hosts: list[str]
    quality: MatchQuality
    permission_level: str | None = None
    display_name: str | None = None

    @property
    def is_usable(self) -> bool:
        """Matched AND readable. A property we cannot read is not a match the
        user can act on, however well the hostname lines up."""
        return self.quality > MatchQuality.NONE and (
            self.permission_level is None
            or self.permission_level in USABLE_PERMISSIONS
        )

    @property
    def proves_ownership(self) -> bool:
        return self.quality >= MatchQuality.URL_PREFIX_HOST and (
            self.permission_level in OWNER_PERMISSIONS
        )


def host_of(url: str) -> str:
    """Host without scheme, port or trailing dot, lowercased."""
    if "://" not in url:
        url = "https://" + url
    host = urlsplit(url).hostname or ""
    return host.rstrip(".").lower()


def bare_domain(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def matched_hosts_for_search_console(site_url: str) -> list[str]:
    """Normalise a Search Console property into the hosts it concerns.

    A domain property is stored as the bare domain; the caller knows it also
    covers every subdomain. A URL-prefix property is stored as its exact host,
    because that is all it covers.
    """
    if site_url.startswith(DOMAIN_PROPERTY_PREFIX):
        return [site_url[len(DOMAIN_PROPERTY_PREFIX) :].rstrip(".").lower()]
    host = host_of(site_url)
    return [host] if host else []


def score_search_console_property(
    website_domain: str, site_url: str, permission_level: str | None = None
) -> PropertyMatch:
    website_domain = bare_domain(website_domain.lower())
    hosts = matched_hosts_for_search_console(site_url)
    quality = MatchQuality.NONE

    if site_url.startswith(DOMAIN_PROPERTY_PREFIX) and hosts:
        domain = hosts[0]
        if website_domain == domain or website_domain.endswith("." + domain):
            quality = MatchQuality.DOMAIN_PROPERTY
    elif hosts:
        host = hosts[0]
        if bare_domain(host) == website_domain:
            quality = MatchQuality.URL_PREFIX_HOST
        elif host.endswith("." + website_domain):
            # e.g. a property for blog.example.com when the website is
            # example.com: related, but not the same site.
            quality = MatchQuality.SUBDOMAIN

    return PropertyMatch(
        property_uri=site_url,
        matched_hosts=hosts,
        quality=quality,
        permission_level=permission_level,
    )


def rank_search_console_matches(
    website_domain: str, site_entries: list[dict[str, str]]
) -> list[PropertyMatch]:
    """Best first. Only usable matches are returned — an unreadable property is
    noise in a list the user has to choose from."""
    matches = [
        score_search_console_property(
            website_domain, entry["siteUrl"], entry.get("permissionLevel")
        )
        for entry in site_entries
        if entry.get("siteUrl")
    ]
    usable = [m for m in matches if m.is_usable]
    return sorted(usable, key=lambda m: (-int(m.quality), m.property_uri))


def score_analytics_property(
    website_domain: str,
    property_uri: str,
    stream_urls: list[str],
    display_name: str | None = None,
) -> PropertyMatch:
    """GA4 properties carry no hostname of their own.

    The match comes from the web data streams' default URIs, which is why
    discovery fetches them: without it the user picks blind from a list that,
    for an agency account, can run to hundreds of identically-named properties.
    """
    website_domain = bare_domain(website_domain.lower())
    hosts = [host_of(u) for u in stream_urls if u]
    hosts = [h for h in hosts if h]

    quality = MatchQuality.NONE
    for host in hosts:
        if bare_domain(host) == website_domain:
            quality = MatchQuality.URL_PREFIX_HOST
            break
        if host.endswith("." + website_domain):
            quality = max(quality, MatchQuality.SUBDOMAIN)

    return PropertyMatch(
        property_uri=property_uri,
        matched_hosts=hosts,
        quality=quality,
        display_name=display_name,
    )
