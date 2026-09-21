"""Repository interfaces.

The domain names what it needs; adapters supply it. This is the seam that
keeps decision 1 real — Supabase is a hosting choice, and if migrating to
managed Postgres meant rewriting domain logic, it would not be one.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from api.domain.models import Membership, Organization, Website


class OrganizationRepository(Protocol):
    async def get(self, organization_id: UUID) -> Organization | None: ...

    async def memberships_for_user(self, user_id: UUID) -> list[Membership]: ...

    async def count_websites(self, organization_id: UUID) -> int: ...


class WebsiteRepository(Protocol):
    async def list_for_organization(self, organization_id: UUID) -> list[Website]: ...

    async def get(self, website_id: UUID) -> Website | None: ...

    async def find_by_domain(
        self, organization_id: UUID, domain: str
    ) -> Website | None: ...

    async def create(
        self,
        *,
        organization_id: UUID,
        domain: str,
        canonical_url: str,
        name: str | None,
    ) -> Website: ...

    async def archive(self, website_id: UUID) -> None: ...

    async def ownership_covers_canonical_url(self, website_id: UUID) -> bool: ...
