"""Stand-in domain objects for tests that render pages.

Real dataclasses, not mocks: a template that reads a field these do not have
should fail the test, which is the whole reason to build them properly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

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

NOW = datetime(2026, 9, 22, 14, 30, tzinfo=UTC)


def project(**kwargs) -> Project:
    base = dict(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        slug="blog",
        name="Blog",
        repo_url="https://github.com/you/blog.git",
        production_branch="main",
        root_directory="",
        framework=None,
        install_command=None,
        build_command=None,
        start_command=None,
        port=None,
        memory_mb=512,
        cpu_shares=1.0,
        keep_warm=2,
        production_deployment_id=None,
        webhook_secret="a-webhook-secret-value",
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(kwargs)
    return Project(**base)


def deployment(**kwargs) -> Deployment:
    base = dict(
        id=uuid4(),
        project_id=UUID("11111111-1111-1111-1111-111111111111"),
        short_id="blog-3f9a2c71",
        number=14,
        status=DeploymentStatus.READY,
        trigger=DeploymentTrigger.PUSH,
        git_sha="4f2a9c1e" + "0" * 32,
        git_ref="main",
        git_message="Rewrite the pricing page",
        git_author="Ada",
        framework="next-standalone",
        image_tag="forge/blog:4f2a9c1e0000",
        internal_port=8080,
        container_id="c" * 64,
        error=None,
        rolled_back_from=None,
        created_at=NOW,
        started_at=NOW,
        built_at=NOW + timedelta(seconds=41),
        ready_at=NOW + timedelta(seconds=44),
        finished_at=NOW + timedelta(seconds=44),
    )
    base.update(kwargs)
    return Deployment(**base)


def env_var(key: str = "DATABASE_URL", target: EnvTarget = EnvTarget.PRODUCTION):
    return EnvVar(
        id=uuid4(),
        project_id=UUID("11111111-1111-1111-1111-111111111111"),
        key=key,
        target=target,
        value_encrypted=b"ciphertext",
        created_at=NOW,
        updated_at=NOW,
    )


def domain(host: str = "example.com", *, verified: bool = True, primary: bool = True):
    return Domain(
        id=uuid4(),
        project_id=UUID("11111111-1111-1111-1111-111111111111"),
        host=host,
        verified_at=NOW if verified else None,
        is_primary=primary,
        created_at=NOW,
    )


def log_line(seq: int, line: str, stream: LogStream = LogStream.BUILD) -> LogLine:
    return LogLine(seq=seq, stream=stream, line=line, at=NOW)
