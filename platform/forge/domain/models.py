"""The vocabulary of the platform.

Plain frozen dataclasses and enums, with no import of psycopg, FastAPI or
docker anywhere in this module — the same vendor-neutral rule the sibling
project enforces on its domain layer, and for the same reason: these types
travel from the database through the engine to the API, and anything they
depend on travels with them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class DeploymentStatus(StrEnum):
    QUEUED = "queued"
    BUILDING = "building"
    DEPLOYING = "deploying"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DeploymentStatus.READY,
            DeploymentStatus.FAILED,
            DeploymentStatus.CANCELLED,
        }

    @property
    def is_in_flight(self) -> bool:
        return self in {DeploymentStatus.BUILDING, DeploymentStatus.DEPLOYING}


class DeploymentTrigger(StrEnum):
    PUSH = "push"
    MANUAL = "manual"
    ROLLBACK = "rollback"
    REDEPLOY = "redeploy"


class EnvTarget(StrEnum):
    PRODUCTION = "production"
    PREVIEW = "preview"
    ALL = "all"

    def covers(self, other: EnvTarget) -> bool:
        return self is EnvTarget.ALL or self is other


class LogStream(StrEnum):
    SYSTEM = "system"
    BUILD = "build"
    RUN = "run"


@dataclass(frozen=True, slots=True)
class Project:
    id: UUID
    slug: str
    name: str
    repo_url: str
    production_branch: str
    root_directory: str
    framework: str | None
    install_command: str | None
    build_command: str | None
    start_command: str | None
    port: int | None
    memory_mb: int
    cpu_shares: float
    keep_warm: int
    production_deployment_id: UUID | None
    webhook_secret: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Deployment:
    id: UUID
    project_id: UUID
    short_id: str
    number: int
    status: DeploymentStatus
    trigger: DeploymentTrigger
    git_sha: str
    git_ref: str
    git_message: str | None
    git_author: str | None
    framework: str | None
    image_tag: str | None
    internal_port: int | None
    container_id: str | None
    error: str | None
    rolled_back_from: UUID | None
    created_at: datetime
    started_at: datetime | None
    built_at: datetime | None
    ready_at: datetime | None
    finished_at: datetime | None

    @property
    def is_live(self) -> bool:
        """Serving right now, as opposed to merely having once succeeded."""
        return self.status is DeploymentStatus.READY and self.container_id is not None


@dataclass(frozen=True, slots=True)
class EnvVar:
    id: UUID
    project_id: UUID
    key: str
    target: EnvTarget
    #: Ciphertext. The plaintext exists only inside the engine, for the few
    #: milliseconds between reading the row and writing the container's env
    #: file, and is never carried on this type.
    value_encrypted: bytes
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Domain:
    id: UUID
    project_id: UUID
    host: str
    verified_at: datetime | None
    is_primary: bool
    created_at: datetime

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None


@dataclass(frozen=True, slots=True)
class LogLine:
    seq: int
    stream: LogStream
    line: str
    at: datetime
