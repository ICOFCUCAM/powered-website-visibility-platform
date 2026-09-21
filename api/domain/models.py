"""Domain models.

Plain dataclasses on purpose: no ORM, no framework types, no vendor SDK. The
architecture contract in docs/08-architecture.md forbids this package from
importing Supabase or a database driver, and CI enforces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"

    @property
    def can_write(self) -> bool:
        return self in (Role.OWNER, Role.ADMIN, Role.MEMBER)

    @property
    def can_administer(self) -> bool:
        return self in (Role.OWNER, Role.ADMIN)


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"
    AGENCY = "agency"
    ENTERPRISE = "enterprise"


class WebsiteStatus(StrEnum):
    PENDING = "PENDING"
    CONNECTING = "CONNECTING"
    CRAWLING = "CRAWLING"
    READY = "READY"
    ERROR = "ERROR"


class OwnershipMethod(StrEnum):
    SEARCH_CONSOLE = "search_console"
    DNS_TXT = "dns_txt"
    FILE_TOKEN = "file_token"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller. Never carries an organisation: scope is
    resolved per request from membership, never accepted from the client."""

    user_id: UUID
    email: str


@dataclass(frozen=True, slots=True)
class Membership:
    organization_id: UUID
    user_id: UUID
    role: Role


@dataclass(frozen=True, slots=True)
class Organization:
    id: UUID
    name: str
    slug: str
    plan: Plan
    max_websites: int
    max_pages_per_crawl: int


@dataclass(frozen=True, slots=True)
class Website:
    id: UUID
    organization_id: UUID
    domain: str
    canonical_url: str
    name: str | None
    status: WebsiteStatus
    timezone: str
    created_at: datetime
    ownership_verified_at: datetime | None = None
    ownership_method: OwnershipMethod | None = None
    crawl_allowed: bool = False
    crawl_blocked_reason: str | None = None

    @property
    def ownership_verified(self) -> bool:
        return self.ownership_verified_at is not None


@dataclass(frozen=True, slots=True)
class ScoreSnapshot:
    as_of: date
    scoring_version: str
    total: float
    components: dict[str, float]
    deterministic: bool = True
