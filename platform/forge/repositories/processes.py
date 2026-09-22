"""Processes and their job runs.

The cron queue works exactly like the deploy queue — a row per unit of work,
claimed with `FOR UPDATE SKIP LOCKED` — with one addition that matters: the
row's identity is the *slot* the schedule named, and the unique constraint on
`(process_id, scheduled_for)` is what makes a slot fire once no matter how
many workers notice it in the same second.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from forge.adapters import db
from forge.domain.errors import Conflict, NotFound
from forge.domain.models import JobRun, Process, ProcessType
from forge.repositories.rows import to_job_run, to_process

COLUMNS = """
    id, project_id, name, type, command, schedule, memory_mb, replicas,
    timeout_seconds, enabled, created_at, updated_at
"""

RUN_COLUMNS = """
    id, process_id, deployment_id, scheduled_for, status, exit_code, detail,
    output, container_id, created_at, started_at, finished_at
"""


async def create(
    *,
    project_id: UUID,
    name: str,
    type: ProcessType,
    command: str | None = None,
    schedule: str | None = None,
    memory_mb: int = 512,
    replicas: int = 1,
    timeout_seconds: int = 900,
) -> Process:
    async with db.connection() as conn:
        try:
            cur = await conn.execute(
                f"""
                INSERT INTO processes (
                    project_id, name, type, command, schedule,
                    memory_mb, replicas, timeout_seconds
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {COLUMNS}
                """,
                (
                    project_id,
                    name,
                    type.value,
                    command,
                    schedule,
                    memory_mb,
                    replicas,
                    timeout_seconds,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            if "processes_project_id_name_key" in str(exc):
                raise Conflict(
                    f"This project already has a process called {name!r}"
                ) from exc
            if "processes_schedule_matches_type" in str(exc):
                raise Conflict(
                    "A cron process needs a schedule, and only a cron process "
                    "may have one."
                ) from exc
            raise
        row = await cur.fetchone()
    return to_process(row)


async def get(process_id: UUID) -> Process:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {COLUMNS} FROM processes WHERE id = %s", (process_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No process with id {process_id}")
    return to_process(row)


async def get_by_name(project_id: UUID, name: str) -> Process:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {COLUMNS} FROM processes WHERE project_id = %s AND name = %s",
            (project_id, name),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No process called {name!r} on this project")
    return to_process(row)


async def list_for_project(project_id: UUID) -> list[Process]:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {COLUMNS} FROM processes WHERE project_id = %s ORDER BY type, name",
            (project_id,),
        )
        rows = await cur.fetchall()
    return [to_process(row) for row in rows]


async def list_long_running(project_id: UUID) -> list[Process]:
    """Workers to reconcile after a promotion. `web` is excluded because the
    deployment pipeline already owns that container."""
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {COLUMNS} FROM processes "
            "WHERE project_id = %s AND type = 'worker' AND enabled "
            "ORDER BY name",
            (project_id,),
        )
        rows = await cur.fetchall()
    return [to_process(row) for row in rows]


async def list_scheduled() -> list[Process]:
    """Every enabled cron process on the platform, for the scheduler sweep."""
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {COLUMNS} FROM processes WHERE type = 'cron' AND enabled "
            "ORDER BY project_id, name"
        )
        rows = await cur.fetchall()
    return [to_process(row) for row in rows]


UPDATABLE = frozenset(
    {"command", "schedule", "memory_mb", "replicas", "timeout_seconds", "enabled"}
)


async def update(process_id: UUID, changes: dict[str, Any]) -> Process:
    fields = {k: v for k, v in changes.items() if k in UPDATABLE}
    if not fields:
        return await get(process_id)
    assignments = ", ".join(f"{name} = %s" for name in fields)
    async with db.connection() as conn:
        cur = await conn.execute(
            f"UPDATE processes SET {assignments} WHERE id = %s RETURNING {COLUMNS}",
            (*fields.values(), process_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No process with id {process_id}")
    return to_process(row)


async def delete(process_id: UUID) -> None:
    async with db.connection() as conn:
        await conn.execute("DELETE FROM processes WHERE id = %s", (process_id,))


# ---------------------------------------------------------------------------
# Job runs
# ---------------------------------------------------------------------------


async def claim_slot(
    process_id: UUID,
    scheduled_for: datetime,
    deployment_id: UUID | None,
    *,
    status: str = "pending",
    detail: str | None = None,
) -> JobRun | None:
    """Record that a slot is due, once.

    `ON CONFLICT DO NOTHING` returning no row means another worker got there
    first — or that this worker already enqueued the slot on a previous sweep.
    Both are the same answer: nothing to do.

    `status` exists so a slot that is already known to be unrunnable can be
    written in its final state rather than as `pending`. Inserting it pending
    and then finishing it leaves a window in which the job loop can claim and
    start a run the sweep is about to mark skipped — harmless in its outcome,
    but a race that does not need to exist.
    """
    terminal = status != "pending"
    async with db.connection() as conn:
        cur = await conn.execute(
            f"""
            INSERT INTO job_runs (
                process_id, scheduled_for, deployment_id, status, detail,
                finished_at
            )
            VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END)
            ON CONFLICT (process_id, scheduled_for) DO NOTHING
            RETURNING {RUN_COLUMNS}
            """,
            (process_id, scheduled_for, deployment_id, status, detail, terminal),
        )
        row = await cur.fetchone()
    return to_job_run(row) if row else None


async def claim_next_run(worker_id: str) -> JobRun | None:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"""
            UPDATE job_runs
               SET status = 'running',
                   leased_by = %s,
                   leased_at = now(),
                   started_at = now()
             WHERE id = (
                   SELECT id FROM job_runs
                    WHERE status = 'pending'
                    ORDER BY scheduled_for
                      FOR UPDATE SKIP LOCKED
                    LIMIT 1
             )
            RETURNING {RUN_COLUMNS}
            """,
            (worker_id,),
        )
        row = await cur.fetchone()
    return to_job_run(row) if row else None


async def finish_run(
    run_id: UUID,
    *,
    status: str,
    exit_code: int | None = None,
    detail: str | None = None,
    output: str | None = None,
) -> JobRun:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"""
            UPDATE job_runs
               SET status = %s, exit_code = %s, detail = %s, output = %s,
                   finished_at = now(), container_id = NULL,
                   leased_by = NULL, leased_at = NULL
             WHERE id = %s
            RETURNING {RUN_COLUMNS}
            """,
            (status, exit_code, detail, output, run_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No job run with id {run_id}")
    return to_job_run(row)


async def set_run_container(run_id: UUID, container_id: str | None) -> None:
    async with db.connection() as conn:
        await conn.execute(
            "UPDATE job_runs SET container_id = %s WHERE id = %s",
            (container_id, run_id),
        )


async def reclaim_abandoned_runs(*, older_than_seconds: int) -> list[JobRun]:
    """Fail runs whose worker stopped reporting.

    Without this a killed worker leaves a run `running` forever, and because
    the slot is already taken the schedule never fires again — a cron job that
    silently stops is worse than one that fails loudly.
    """
    async with db.connection() as conn:
        cur = await conn.execute(
            f"""
            UPDATE job_runs
               SET status = 'failed',
                   detail = 'The worker running this job stopped responding.',
                   finished_at = now(),
                   leased_by = NULL, leased_at = NULL
             WHERE status = 'running'
               AND leased_at < now() - make_interval(secs => %s)
            RETURNING {RUN_COLUMNS}
            """,
            (older_than_seconds,),
        )
        rows = await cur.fetchall()
    return [to_job_run(row) for row in rows]


async def list_runs(process_id: UUID, *, limit: int = 25) -> list[JobRun]:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"SELECT {RUN_COLUMNS} FROM job_runs WHERE process_id = %s "
            "ORDER BY scheduled_for DESC LIMIT %s",
            (process_id, limit),
        )
        rows = await cur.fetchall()
    return [to_job_run(row) for row in rows]


async def last_run(process_id: UUID) -> JobRun | None:
    runs = await list_runs(process_id, limit=1)
    return runs[0] if runs else None
