"""Resource discovery.

One pass after consent, across every granted service. Everything lands in
`connection_properties` with `matched_hosts` normalised, which is what the
wizard's "choose your website" step renders and what auto-matching reads.

GA4 needs a second call per property to find its web streams, because an
Analytics property carries no hostname of its own. That is the difference
between "we found yours" and an unsorted list of several hundred properties
named "GA4".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from uuid import UUID

from api.hub.providers.google.client import GoogleClient
from api.hub.providers.google.errors import GoogleError
from api.hub.repositories import HubRepository
from api.hub.services.matching import matched_hosts_for_search_console

logger = logging.getLogger("visibility_hub.discovery")

#: Fetching streams for every property in an agency account would be hundreds
#: of calls against a quota shared with syncing. Bounded, newest first.
MAX_ANALYTICS_STREAM_LOOKUPS = 40


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    search_console: int = 0
    analytics: int = 0
    skipped_stream_lookups: int = 0


class DiscoveryService:
    def __init__(self, repo: HubRepository, client: GoogleClient) -> None:
        self._repo = repo
        self._client = client

    async def discover(
        self,
        *,
        organization_id: UUID,
        connection_id: UUID,
        access_token: str,
        services: list[str],
    ) -> DiscoveryResult:
        search_console = analytics = skipped = 0

        if "search_console" in services:
            search_console = await self._discover_search_console(
                organization_id, connection_id, access_token
            )

        if "analytics" in services:
            analytics, skipped = await self._discover_analytics(
                organization_id, connection_id, access_token
            )

        return DiscoveryResult(search_console, analytics, skipped)

    async def _discover_search_console(
        self, organization_id: UUID, connection_id: UUID, access_token: str
    ) -> int:
        entries = await self._client.list_search_console_sites(access_token)
        count = 0
        for entry in entries:
            site_url = entry.get("siteUrl")
            if not site_url:
                continue
            await self._repo.upsert_property(
                organization_id=organization_id,
                connection_id=connection_id,
                provider_key="google",
                service="search_console",
                property_uri=site_url,
                property_name=site_url,
                permission_level=entry.get("permissionLevel"),
                matched_hosts=matched_hosts_for_search_console(site_url),
                raw=json.dumps(entry),
            )
            count += 1
        return count

    async def _discover_analytics(
        self, organization_id: UUID, connection_id: UUID, access_token: str
    ) -> tuple[int, int]:
        properties = await self._client.list_analytics_properties(access_token)
        count = 0
        skipped = 0

        for index, prop in enumerate(properties):
            property_uri = prop.get("property")
            if not property_uri:
                continue

            hosts: list[str] = []
            if index < MAX_ANALYTICS_STREAM_LOOKUPS:
                try:
                    urls = await self._client.list_web_stream_urls(
                        access_token, property_uri
                    )
                    hosts = [
                        h
                        for h in (_host_of(u) for u in urls)
                        if h
                    ]
                except GoogleError:
                    # A stream lookup failing must not abandon discovery: the
                    # property is still listed, just without an auto-match.
                    logger.info("stream lookup failed for a property")
            else:
                skipped += 1

            await self._repo.upsert_property(
                organization_id=organization_id,
                connection_id=connection_id,
                provider_key="google",
                service="analytics",
                property_uri=property_uri,
                property_name=prop.get("displayName"),
                permission_level=None,
                matched_hosts=hosts,
                raw=json.dumps(prop),
            )
            count += 1

        return count, skipped


def _host_of(url: str) -> str:
    from api.hub.services.matching import host_of

    return host_of(url)
