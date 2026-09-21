"""Verifying a website in tests, the way the product actually verifies one.

Setting `ownership_verified_at` alone is not enough and should not be: the
crawler asks `website_ownership_evidence`, which requires a Search Console
property that COVERS the canonical URL and is held as owner. A test helper
that skipped that would quietly bypass the gate it is meant to respect.
"""

from __future__ import annotations

import uuid


async def verify_website(conn, organization_id, website_id, *,
                         property_uri: str = "sc-domain:example.com",
                         permission_level: str = "siteOwner") -> uuid.UUID:
    connection_id, property_id = uuid.uuid4(), uuid.uuid4()

    await conn.execute(
        "insert into connections (id, organization_id, provider_key, external_id, "
        "  label) values (%s,%s,'google',%s,'owner@example.com')",
        (connection_id, organization_id, uuid.uuid4().hex),
    )
    await conn.execute(
        "insert into connection_properties (id, organization_id, connection_id, "
        "  provider_key, service, property_uri, permission_level, matched_hosts) "
        "values (%s,%s,%s,'google','search_console',%s,%s,'{example.com}')",
        (property_id, organization_id, connection_id, property_uri, permission_level),
    )
    await conn.execute(
        "insert into website_connections (organization_id, website_id, property_id, "
        "  provider_key, service) values (%s,%s,%s,'google','search_console')",
        (organization_id, website_id, property_id),
    )
    await conn.execute(
        "update websites set ownership_verified_at = now(), "
        "  ownership_method = 'search_console', ownership_property_id = %s "
        " where id = %s",
        (property_id, website_id),
    )
    return property_id
