"""Request and response bodies.

Responses never carry a secret. The environment variable endpoints return keys
and scopes but no values — not even encrypted ones — because an API that will
read a secret back to you is an API that will read it back to anything holding
the token, and the platform has no reason to ever need that direction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from forge.domain.models import (
    Deployment,
    Domain,
    EnvTarget,
    JobRun,
    Process,
    ProcessType,
    Project,
)

SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$"


class CreateProject(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    repo_url: str = Field(min_length=1)
    slug: str | None = Field(default=None, pattern=SLUG_PATTERN)
    production_branch: str = "main"
    root_directory: str = ""
    framework: str | None = None
    install_command: str | None = None
    build_command: str | None = None
    start_command: str | None = None
    port: int | None = Field(default=None, gt=0, lt=65536)
    memory_mb: int = Field(default=512, ge=64, le=65536)
    cpu_shares: float = Field(default=1.0, gt=0, le=64)


class UpdateProject(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    repo_url: str | None = None
    production_branch: str | None = None
    root_directory: str | None = None
    framework: str | None = None
    install_command: str | None = None
    build_command: str | None = None
    start_command: str | None = None
    port: int | None = Field(default=None, gt=0, lt=65536)
    memory_mb: int | None = Field(default=None, ge=64, le=65536)
    cpu_shares: float | None = Field(default=None, gt=0, le=64)
    keep_warm: int | None = Field(default=None, ge=0, le=50)

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True, exclude_none=True)


class DeployRequest(BaseModel):
    ref: str | None = None
    sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class SetEnv(BaseModel):
    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128)
    value: str = Field(max_length=32_768)
    target: EnvTarget = EnvTarget.ALL


class AddDomain(BaseModel):
    host: str = Field(min_length=3, max_length=253)
    primary: bool = False

    @field_validator("host")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = value.strip().lower().rstrip(".")
        if "/" in cleaned or ":" in cleaned:
            raise ValueError("Give a hostname, not a URL")
        if "." not in cleaned:
            raise ValueError("That is not a fully qualified domain name")
        return cleaned


class ProjectOut(BaseModel):
    id: UUID
    slug: str
    name: str
    repo_url: str
    production_branch: str
    root_directory: str
    framework: str | None
    port: int | None
    memory_mb: int
    cpu_shares: float
    keep_warm: int
    production_deployment_id: UUID | None
    created_at: datetime

    @classmethod
    def of(cls, project: Project) -> ProjectOut:
        return cls(
            id=project.id,
            slug=project.slug,
            name=project.name,
            repo_url=project.repo_url,
            production_branch=project.production_branch,
            root_directory=project.root_directory,
            framework=project.framework,
            port=project.port,
            memory_mb=project.memory_mb,
            cpu_shares=project.cpu_shares,
            keep_warm=project.keep_warm,
            production_deployment_id=project.production_deployment_id,
            created_at=project.created_at,
        )


class DeploymentOut(BaseModel):
    id: UUID
    project_id: UUID
    short_id: str
    number: int
    status: str
    trigger: str
    git_sha: str
    git_ref: str
    git_message: str | None
    git_author: str | None
    framework: str | None
    error: str | None
    url: str
    is_live: bool
    is_production: bool
    created_at: datetime
    ready_at: datetime | None

    @classmethod
    def of(
        cls, deployment: Deployment, *, url: str, is_production: bool
    ) -> DeploymentOut:
        return cls(
            id=deployment.id,
            project_id=deployment.project_id,
            short_id=deployment.short_id,
            number=deployment.number,
            status=deployment.status.value,
            trigger=deployment.trigger.value,
            git_sha=deployment.git_sha,
            git_ref=deployment.git_ref,
            git_message=deployment.git_message,
            git_author=deployment.git_author,
            framework=deployment.framework,
            error=deployment.error,
            url=url,
            is_live=deployment.is_live,
            is_production=is_production,
            created_at=deployment.created_at,
            ready_at=deployment.ready_at,
        )


class EnvOut(BaseModel):
    """Key and scope only. The value never comes back out."""

    key: str
    target: str
    updated_at: datetime


class DomainOut(BaseModel):
    id: UUID
    host: str
    is_primary: bool
    verified: bool
    verified_at: datetime | None

    @classmethod
    def of(cls, domain: Domain) -> DomainOut:
        return cls(
            id=domain.id,
            host=domain.host,
            is_primary=domain.is_primary,
            verified=domain.is_verified,
            verified_at=domain.verified_at,
        )


class LogOut(BaseModel):
    seq: int
    stream: str
    line: str
    at: datetime


class WebhookAccepted(BaseModel):
    deployment: DeploymentOut | None = None
    ignored: str | None = None


class CreateProcess(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
    type: ProcessType
    command: str | None = None
    schedule: str | None = None
    memory_mb: int = Field(default=512, ge=64, le=65536)
    replicas: int = Field(default=1, ge=0, le=20)
    timeout_seconds: int = Field(default=900, gt=0, le=86_400)


class UpdateProcess(BaseModel):
    command: str | None = None
    schedule: str | None = None
    memory_mb: int | None = Field(default=None, ge=64, le=65536)
    replicas: int | None = Field(default=None, ge=0, le=20)
    timeout_seconds: int | None = Field(default=None, gt=0, le=86_400)
    enabled: bool | None = None

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class ProcessOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    type: str
    command: str | None
    schedule: str | None
    schedule_description: str | None
    next_run_at: datetime | None
    memory_mb: int
    replicas: int
    timeout_seconds: int
    enabled: bool

    @classmethod
    def of(
        cls,
        process: Process,
        *,
        description: str | None = None,
        next_run_at: datetime | None = None,
    ) -> ProcessOut:
        return cls(
            id=process.id,
            project_id=process.project_id,
            name=process.name,
            type=process.type.value,
            command=process.command,
            schedule=process.schedule,
            schedule_description=description,
            next_run_at=next_run_at,
            memory_mb=process.memory_mb,
            replicas=process.replicas,
            timeout_seconds=process.timeout_seconds,
            enabled=process.enabled,
        )


class JobRunOut(BaseModel):
    id: UUID
    process_id: UUID
    scheduled_for: datetime
    status: str
    exit_code: int | None
    detail: str | None
    duration_seconds: float | None
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, run: JobRun) -> JobRunOut:
        return cls(
            id=run.id,
            process_id=run.process_id,
            scheduled_for=run.scheduled_for,
            status=run.status.value,
            exit_code=run.exit_code,
            detail=run.detail,
            duration_seconds=run.duration_seconds,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )


class JobRunDetail(JobRunOut):
    """A single run, with the container's output.

    Kept off the list response on purpose: the tail of one job's output is a
    useful page and twenty-five of them is a download.
    """

    output: str | None = None

    @classmethod
    def of(cls, run: JobRun) -> JobRunDetail:
        base = JobRunOut.of(run).model_dump()
        return cls(**base, output=run.output)
