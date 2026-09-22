"""Row dictionaries to domain objects.

One place, so that adding a column means changing one mapper rather than
hunting every query that selects `*`.
"""

from __future__ import annotations

from typing import Any

from forge.domain.models import (
    Deployment,
    DeploymentStatus,
    DeploymentTrigger,
    Domain,
    EnvTarget,
    EnvVar,
    LogLine,
    LogStream,
    Project,
)


def to_project(row: dict[str, Any]) -> Project:
    return Project(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        repo_url=row["repo_url"],
        production_branch=row["production_branch"],
        root_directory=row["root_directory"],
        framework=row["framework"],
        install_command=row["install_command"],
        build_command=row["build_command"],
        start_command=row["start_command"],
        port=row["port"],
        memory_mb=row["memory_mb"],
        cpu_shares=float(row["cpu_shares"]),
        keep_warm=row["keep_warm"],
        production_deployment_id=row["production_deployment_id"],
        webhook_secret=row["webhook_secret"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def to_deployment(row: dict[str, Any]) -> Deployment:
    return Deployment(
        id=row["id"],
        project_id=row["project_id"],
        short_id=row["short_id"],
        number=row["number"],
        status=DeploymentStatus(row["status"]),
        trigger=DeploymentTrigger(row["trigger"]),
        git_sha=row["git_sha"],
        git_ref=row["git_ref"],
        git_message=row["git_message"],
        git_author=row["git_author"],
        framework=row["framework"],
        image_tag=row["image_tag"],
        internal_port=row["internal_port"],
        container_id=row["container_id"],
        error=row["error"],
        rolled_back_from=row["rolled_back_from"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        built_at=row["built_at"],
        ready_at=row["ready_at"],
        finished_at=row["finished_at"],
    )


def to_env_var(row: dict[str, Any]) -> EnvVar:
    return EnvVar(
        id=row["id"],
        project_id=row["project_id"],
        key=row["key"],
        target=EnvTarget(row["target"]),
        value_encrypted=bytes(row["value_encrypted"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def to_domain(row: dict[str, Any]) -> Domain:
    return Domain(
        id=row["id"],
        project_id=row["project_id"],
        host=row["host"],
        verified_at=row["verified_at"],
        is_primary=row["is_primary"],
        created_at=row["created_at"],
    )


def to_log_line(row: dict[str, Any]) -> LogLine:
    return LogLine(
        seq=row["seq"],
        stream=LogStream(row["stream"]),
        line=row["line"],
        at=row["at"],
    )
