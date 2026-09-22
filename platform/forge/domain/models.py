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


class ProcessType(StrEnum):
    WEB = "web"
    WORKER = "worker"
    CRON = "cron"

    @property
    def is_long_running(self) -> bool:
        """Whether this process wants a container that stays up.

        Cron is the exception: its container is created per slot, runs once
        and is removed, so nothing about it is reconciled against "should be
        running right now".
        """
        return self in {ProcessType.WEB, ProcessType.WORKER}


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"

    @property
    def is_terminal(self) -> bool:
        return self not in {JobStatus.PENDING, JobStatus.RUNNING}


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


@dataclass(frozen=True, slots=True)
class Process:
    """Something a project runs besides its website.

    Built from the same image as the deployment, so a worker and the web app
    are running the same code by construction rather than by anyone
    remembering to deploy both.
    """

    id: UUID
    project_id: UUID
    name: str
    type: ProcessType
    #: None means the image's own CMD.
    command: str | None
    #: Five-field cron in UTC, and only ever set on a cron process.
    schedule: str | None
    memory_mb: int
    replicas: int
    timeout_seconds: int
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @property
    def runs_on_a_schedule(self) -> bool:
        return self.type is ProcessType.CRON and self.schedule is not None


@dataclass(frozen=True, slots=True)
class JobRun:
    """One execution of a cron process, identified by the slot it claimed."""

    id: UUID
    process_id: UUID
    deployment_id: UUID | None
    #: The minute the schedule named — not the minute the container started.
    #: A run that began four minutes late is still that slot's run.
    scheduled_for: datetime
    status: JobStatus
    exit_code: int | None
    detail: str | None
    output: str | None
    container_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at or not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()
