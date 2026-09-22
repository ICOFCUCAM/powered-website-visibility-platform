"""Projects, their environment variables and their domains."""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

from forge.adapters import db
from forge.domain.errors import Conflict, NotFound
from forge.domain.models import Domain, EnvTarget, EnvVar, Project
from forge.repositories.rows import to_domain, to_env_var, to_project

PROJECT_COLUMNS = """
    id, slug, name, repo_url, production_branch, root_directory,
    framework, install_command, build_command, start_command, port,
    memory_mb, cpu_shares, keep_warm, production_deployment_id,
    webhook_secret, created_at, updated_at
"""


async def create(
    *,
    slug: str,
    name: str,
    repo_url: str,
    production_branch: str = "main",
    root_directory: str = "",
    framework: str | None = None,
    install_command: str | None = None,
    build_command: str | None = None,
    start_command: str | None = None,
    port: int | None = None,
    memory_mb: int = 512,
    cpu_shares: float = 1.0,
) -> Project:
    async with db.connection() as conn:
        try:
            cur = await conn.execute(
                f"""
                INSERT INTO projects (
                    slug, name, repo_url, production_branch, root_directory,
                    framework, install_command, build_command, start_command,
                    port, memory_mb, cpu_shares, webhook_secret
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {PROJECT_COLUMNS}
                """,
                (
                    slug,
                    name,
                    repo_url,
                    production_branch,
                    root_directory.strip("/"),
                    framework,
                    install_command,
                    build_command,
                    start_command,
                    port,
                    memory_mb,
                    cpu_shares,
                    secrets.token_urlsafe(32),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            if "projects_slug_key" in str(exc):
                raise Conflict(f"A project named {slug!r} already exists") from exc
            raise
        row = await cur.fetchone()
    return to_project(row)


async def get(project_id: UUID) -> Project:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id = %s", (project_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No project with id {project_id}")
    return to_project(row)


async def get_by_slug(slug: str) -> Project:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {PROJECT_COLUMNS} FROM projects WHERE slug = %s", (slug,)
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No project named {slug!r}")
    return to_project(row)


async def resolve(reference: str) -> Project:
    """Accept either a slug or a uuid, because both appear in the CLI."""
    try:
        return await get(UUID(reference))
    except ValueError:
        return await get_by_slug(reference)


async def list_all() -> list[Project]:
    async with db.connection() as conn:
        cur = await conn.execute(f"SELECT {PROJECT_COLUMNS} FROM projects ORDER BY name")
        rows = await cur.fetchall()
    return [to_project(row) for row in rows]


#: Columns a caller may change through `update`. Anything not named here is
#: either immutable (slug) or owned by the engine (production_deployment_id),
#: and letting an API body reach them is how a project ends up pointing at
#: another project's deployment.
UPDATABLE = frozenset(
    {
        "name",
        "repo_url",
        "production_branch",
        "root_directory",
        "framework",
        "install_command",
        "build_command",
        "start_command",
        "port",
        "memory_mb",
        "cpu_shares",
        "keep_warm",
    }
)


async def update(project_id: UUID, changes: dict[str, Any]) -> Project:
    fields = {k: v for k, v in changes.items() if k in UPDATABLE}
    if not fields:
        return await get(project_id)
    assignments = ", ".join(f"{name} = %s" for name in fields)
    async with db.connection() as conn:
        cur = await conn.execute(
            f"UPDATE projects SET {assignments} WHERE id = %s "
            f"RETURNING {PROJECT_COLUMNS}",
            (*fields.values(), project_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No project with id {project_id}")
    return to_project(row)


async def set_production(project_id: UUID, deployment_id: UUID | None) -> None:
    async with db.connection() as conn:
        await conn.execute(
            "UPDATE projects SET production_deployment_id = %s WHERE id = %s",
            (deployment_id, project_id),
        )


async def delete(project_id: UUID) -> None:
    async with db.connection() as conn:
        # The pointer has to go first: it is a foreign key into a table that
        # cascades from this row, and Postgres will not let the cascade run
        # while the parent still references a child.
        await conn.execute(
            "UPDATE projects SET production_deployment_id = NULL WHERE id = %s",
            (project_id,),
        )
        await conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

ENV_COLUMNS = "id, project_id, key, target, value_encrypted, created_at, updated_at"


async def set_env(
    project_id: UUID, key: str, value_encrypted: bytes, target: EnvTarget
) -> EnvVar:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"""
            INSERT INTO env_vars (project_id, key, target, value_encrypted)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (project_id, key, target)
            DO UPDATE SET value_encrypted = EXCLUDED.value_encrypted
            RETURNING {ENV_COLUMNS}
            """,
            (project_id, key, target.value, value_encrypted),
        )
        row = await cur.fetchone()
    return to_env_var(row)


async def list_env(project_id: UUID) -> list[EnvVar]:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {ENV_COLUMNS} FROM env_vars WHERE project_id = %s "
            "ORDER BY key, target",
            (project_id,),
        )
        rows = await cur.fetchall()
    return [to_env_var(row) for row in rows]


async def delete_env(project_id: UUID, key: str, target: EnvTarget | None) -> int:
    async with db.connection() as conn:
        if target is None:
            cur = await conn.execute(
                "DELETE FROM env_vars WHERE project_id = %s AND key = %s",
                (project_id, key),
            )
        else:
            cur = await conn.execute(
                "DELETE FROM env_vars WHERE project_id = %s AND key = %s AND target = %s",
                (project_id, key, target.value),
            )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------

DOMAIN_COLUMNS = "id, project_id, host, verified_at, is_primary, created_at"


async def add_domain(project_id: UUID, host: str, *, primary: bool) -> Domain:
    normalised = host.strip().lower().rstrip(".")
    async with db.transaction() as conn:
        if primary:
            await conn.execute(
                "UPDATE domains SET is_primary = false WHERE project_id = %s",
                (project_id,),
            )
        try:
            cur = await conn.execute(
                f"""
                INSERT INTO domains (project_id, host, is_primary)
                VALUES (%s, %s, %s)
                RETURNING {DOMAIN_COLUMNS}
                """,
                (project_id, normalised, primary),
            )
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            if "domains_host_key" in str(exc):
                raise Conflict(f"{normalised} is already attached to a project") from exc
            raise
        row = await cur.fetchone()
    return to_domain(row)


async def list_domains(project_id: UUID) -> list[Domain]:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {DOMAIN_COLUMNS} FROM domains WHERE project_id = %s "
            "ORDER BY is_primary DESC, host",
            (project_id,),
        )
        rows = await cur.fetchall()
    return [to_domain(row) for row in rows]


async def mark_domain_verified(domain_id: UUID) -> Domain:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"UPDATE domains SET verified_at = now() WHERE id = %s "
            f"RETURNING {DOMAIN_COLUMNS}",
            (domain_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No domain with id {domain_id}")
    return to_domain(row)


async def remove_domain(project_id: UUID, host: str) -> int:
    async with db.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM domains WHERE project_id = %s AND host = %s",
            (project_id, host.strip().lower()),
        )
        return cur.rowcount
