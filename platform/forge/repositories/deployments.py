"""Deployments, their queue and their logs.

The queue is this table. There is no broker, for the same reason the sibling
project leases its crawl frontier out of Postgres: the work is already in a
transactional store, and a second system holding "which deployment is being
built" introduces a way for the two to disagree that no amount of care removes.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from psycopg.errors import UniqueViolation

from forge.adapters import db
from forge.domain.errors import Conflict, NotFound
from forge.domain.models import (
    Deployment,
    DeploymentStatus,
    DeploymentTrigger,
    LogLine,
    LogStream,
)
from forge.repositories.rows import to_deployment, to_log_line

COLUMNS = """
    id, project_id, short_id, number, status, trigger, git_sha, git_ref,
    git_message, git_author, framework, image_tag, internal_port,
    container_id, error, rolled_back_from, created_at, started_at,
    built_at, ready_at, finished_at
"""


async def create(
    *,
    project_id: UUID,
    short_id: str,
    git_sha: str,
    git_ref: str,
    trigger: DeploymentTrigger,
    git_message: str | None = None,
    git_author: str | None = None,
    rolled_back_from: UUID | None = None,
    attempts: int = 3,
) -> Deployment:
    """Queue a deployment, numbering it one past the project's highest.

    The number is computed inside the INSERT rather than read first, so two
    simultaneous pushes cannot both read 7 and both write 8. They can still
    collide on the unique constraint, which is what the retry is for — a loop
    of three is ample for a queue whose realistic peak is two pushes landing in
    the same second.
    """
    for attempt in range(attempts):
        try:
            async with db.connection() as conn:
                cur = await conn.execute(
                    f"""
                    INSERT INTO deployments (
                        project_id, short_id, number, git_sha, git_ref,
                        trigger, git_message, git_author, rolled_back_from
                    )
                    SELECT
                        %s, %s,
                        COALESCE(MAX(number), 0) + 1,
                        %s, %s, %s, %s, %s, %s
                    FROM deployments WHERE project_id = %s
                    RETURNING {COLUMNS}
                    """,
                    (
                        project_id,
                        short_id,
                        git_sha,
                        git_ref,
                        trigger.value,
                        git_message,
                        git_author,
                        rolled_back_from,
                        project_id,
                    ),
                )
                row = await cur.fetchone()
            return to_deployment(row)
        except UniqueViolation:
            if attempt == attempts - 1:
                raise
    raise AssertionError("unreachable")


async def get(deployment_id: UUID) -> Deployment:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {COLUMNS} FROM deployments WHERE id = %s", (deployment_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No deployment with id {deployment_id}")
    return to_deployment(row)


async def get_by_short_id(short_id: str) -> Deployment:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {COLUMNS} FROM deployments WHERE short_id = %s", (short_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No deployment {short_id!r}")
    return to_deployment(row)


async def list_for_project(project_id: UUID, *, limit: int = 50) -> list[Deployment]:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {COLUMNS} FROM deployments WHERE project_id = %s "
            "ORDER BY number DESC LIMIT %s",
            (project_id, limit),
        )
        rows = await cur.fetchall()
    return [to_deployment(row) for row in rows]


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


async def claim_next(worker_id: str) -> Deployment | None:
    """Take the oldest queued deployment, or return None.

    `FOR UPDATE SKIP LOCKED` is what makes running several workers safe: each
    one locks a different row instead of queueing behind the same one, and a
    worker that dies mid-transaction releases its lock rather than blocking the
    others.
    """
    async with db.connection() as conn:
        cur = await conn.execute(
            f"""
            UPDATE deployments
               SET status = 'building',
                   leased_by = %s,
                   leased_at = now(),
                   started_at = now()
             WHERE id = (
                   SELECT id FROM deployments
                    WHERE status = 'queued'
                    ORDER BY created_at
                      FOR UPDATE SKIP LOCKED
                    LIMIT 1
             )
            RETURNING {COLUMNS}
            """,
            (worker_id,),
        )
        row = await cur.fetchone()
    return to_deployment(row) if row else None


async def reclaim_abandoned(*, older_than_seconds: int) -> list[Deployment]:
    """Fail deployments whose worker stopped reporting.

    A worker killed mid-build leaves a row claiming to be building forever.
    Nothing else will ever touch it, the project's deploy list shows a spinner
    that never resolves, and — worse — a queued deployment behind it looks
    fine. The lease timestamp is what makes that recoverable without a human.
    """
    async with db.connection() as conn:
        cur = await conn.execute(
            f"""
            UPDATE deployments
               SET status = 'failed',
                   error = 'The worker building this deployment stopped '
                           'responding. Nothing was deployed; try again.',
                   finished_at = now(),
                   leased_by = NULL,
                   leased_at = NULL
             WHERE status IN ('building', 'deploying')
               AND leased_at < now() - make_interval(secs => %s)
            RETURNING {COLUMNS}
            """,
            (older_than_seconds,),
        )
        rows = await cur.fetchall()
    return [to_deployment(row) for row in rows]


async def heartbeat(deployment_id: UUID) -> None:
    """Push the lease forward while real work is happening.

    Called between phases rather than on a timer. A build that legitimately
    takes twenty minutes must not be reclaimed, and the honest signal that it
    is alive is that it reached the next phase.
    """
    async with db.connection() as conn:
        await conn.execute(
            "UPDATE deployments SET leased_at = now() WHERE id = %s", (deployment_id,)
        )


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


async def mark_built(
    deployment_id: UUID, *, image_tag: str, framework: str, internal_port: int
) -> Deployment:
    return await _update(
        deployment_id,
        """
        status = 'deploying', image_tag = %s, framework = %s,
        internal_port = %s, built_at = now()
        """,
        (image_tag, framework, internal_port),
    )


async def mark_ready(deployment_id: UUID, *, container_id: str) -> Deployment:
    return await _update(
        deployment_id,
        """
        status = 'ready', container_id = %s, error = NULL,
        ready_at = now(), finished_at = now(),
        leased_by = NULL, leased_at = NULL
        """,
        (container_id,),
    )


async def mark_failed(deployment_id: UUID, *, error: str) -> Deployment:
    return await _update(
        deployment_id,
        """
        status = 'failed', error = %s, finished_at = now(),
        leased_by = NULL, leased_at = NULL
        """,
        (error[:2000],),
    )


async def mark_cancelled(deployment_id: UUID) -> Deployment:
    return await _update(
        deployment_id,
        """
        status = 'cancelled', finished_at = now(),
        leased_by = NULL, leased_at = NULL
        """,
        (),
    )


async def set_container(deployment_id: UUID, container_id: str | None) -> None:
    """Record that the container exists, or no longer does.

    Detaching a container does not change the deployment's status: it stayed
    ready, it simply is not resident. Rollback restarts it from the image.
    """
    async with db.connection() as conn:
        await conn.execute(
            "UPDATE deployments SET container_id = %s WHERE id = %s",
            (container_id, deployment_id),
        )


async def _update(deployment_id: UUID, assignments: str, params: tuple) -> Deployment:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"UPDATE deployments SET {assignments} WHERE id = %s RETURNING {COLUMNS}",
            (*params, deployment_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No deployment with id {deployment_id}")
    return to_deployment(row)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def reclaimable(
    project_id: UUID, *, keep: int, protect: Iterable[UUID]
) -> list[Deployment]:
    """Ready deployments whose containers may be stopped to free memory.

    Protecting the production deployment is not left to ordering: it is passed
    in explicitly, because "the newest N" stops being the right set the moment
    someone rolls back to an old one.
    """
    protected = list(protect)
    async with db.connection() as conn:
        cur = await conn.execute(
            f"""
            SELECT {COLUMNS} FROM deployments
             WHERE project_id = %s
               AND status = 'ready'
               AND container_id IS NOT NULL
               AND NOT (id = ANY(%s::uuid[]))
             ORDER BY number DESC
            OFFSET %s
            """,
            (project_id, protected, keep),
        )
        rows = await cur.fetchall()
    return [to_deployment(row) for row in rows]


async def ensure_promotable(deployment_id: UUID, project_id: UUID) -> Deployment:
    """The checks promotion must not skip.

    Both failures here are real and neither is hypothetical: promoting a failed
    deployment would route production at a container that never started, and
    promoting another project's deployment is one mistyped id away in any API
    that takes both as path parameters.
    """
    deployment = await get(deployment_id)
    if deployment.project_id != project_id:
        raise Conflict("That deployment belongs to a different project")
    if deployment.status is not DeploymentStatus.READY:
        raise Conflict(
            f"Deployment #{deployment.number} is {deployment.status.value}, "
            "so it cannot serve production. Only a ready deployment can."
        )
    return deployment


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


async def append_logs(
    deployment_id: UUID, entries: list[tuple[int, LogStream, str]]
) -> None:
    """Insert a batch of log lines.

    Batched because a Docker build emits hundreds of lines in a few seconds and
    a round trip each would make the build slower than the build.
    """
    if not entries:
        return
    async with db.connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            """
                INSERT INTO deployment_logs (deployment_id, seq, stream, line)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (deployment_id, seq) DO NOTHING
                """,
            [(deployment_id, seq, stream.value, line) for seq, stream, line in entries],
        )


async def read_logs(
    deployment_id: UUID, *, after: int = 0, limit: int = 2000
) -> list[LogLine]:
    async with db.connection() as conn:
        cur = await conn.execute(
            """
            SELECT seq, stream, line, at FROM deployment_logs
             WHERE deployment_id = %s AND seq > %s
             ORDER BY seq LIMIT %s
            """,
            (deployment_id, after, limit),
        )
        rows = await cur.fetchall()
    return [to_log_line(row) for row in rows]


async def last_log_seq(deployment_id: UUID) -> int:
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM deployment_logs "
            "WHERE deployment_id = %s",
            (deployment_id,),
        )
        row = await cur.fetchone()
    return row["seq"]
