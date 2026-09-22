"""Workers and scheduled jobs over HTTP."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Response, status

from forge.deps import Authenticated, ProjectDep
from forge.domain.errors import Conflict, InvalidRequest
from forge.domain.models import ProcessType
from forge.domain.schedule import InvalidSchedule, describe, parse
from forge.engine import processes as process_engine
from forge.repositories import processes as process_repo
from forge.routers.schemas import (
    CreateProcess,
    JobRunDetail,
    JobRunOut,
    ProcessOut,
    UpdateProcess,
)

project_router = APIRouter(
    prefix="/api/projects", tags=["processes"], dependencies=[Authenticated]
)
router = APIRouter(
    prefix="/api/processes", tags=["processes"], dependencies=[Authenticated]
)


@project_router.get("/{project_ref}/processes")
async def list_processes(project: ProjectDep) -> list[ProcessOut]:
    return [_out(p) for p in await process_repo.list_for_project(project.id)]


@project_router.post("/{project_ref}/processes", status_code=status.HTTP_201_CREATED)
async def create_process(project: ProjectDep, body: CreateProcess) -> ProcessOut:
    schedule = _validate(body.type, body.schedule)
    if body.type is ProcessType.WEB:
        raise InvalidRequest(
            "The web process is the deployment itself and is not created here."
        )
    process = await process_repo.create(
        project_id=project.id,
        name=body.name,
        type=body.type,
        command=body.command,
        schedule=schedule,
        memory_mb=body.memory_mb,
        replicas=body.replicas,
        timeout_seconds=body.timeout_seconds,
    )
    return _out(process)


@router.get("/{process_id}")
async def get_process(process_id: UUID) -> ProcessOut:
    return _out(await process_repo.get(process_id))


@router.patch("/{process_id}")
async def update_process(process_id: UUID, body: UpdateProcess) -> ProcessOut:
    process = await process_repo.get(process_id)
    changes = body.changes()
    if "schedule" in changes:
        changes["schedule"] = _validate(process.type, changes["schedule"])
    return _out(await process_repo.update(process_id, changes))


@router.delete("/{process_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_process(process_id: UUID) -> Response:
    """Remove the process. Its containers go on the next promotion.

    Not stopped here: doing it in the request would make a slow `docker stop`
    part of a call the caller is waiting on, and reconciliation already
    removes any worker container the database no longer wants.
    """
    await process_repo.delete(process_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{process_id}/runs")
async def list_runs(process_id: UUID, limit: int = 25) -> list[JobRunOut]:
    return [
        JobRunOut.of(run)
        for run in await process_repo.list_runs(process_id, limit=min(limit, 200))
    ]


@router.post("/{process_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_now(process_id: UUID) -> JobRunDetail:
    """Queue this job immediately, outside its schedule.

    The manual run occupies the current minute's slot, so it cannot produce a
    second execution of a schedule that was about to fire anyway — pressing
    "run now" at 02:59:58 on a job due at 03:00 gets one run, not two.
    """
    process = await process_repo.get(process_id)
    if process.type is not ProcessType.CRON:
        raise Conflict(f"{process.name!r} is not a scheduled job")

    from forge.repositories import projects as project_repo

    project = await project_repo.get(process.project_id)
    slot = datetime.now(UTC).replace(second=0, microsecond=0)
    run = await process_repo.claim_slot(
        process.id, slot, project.production_deployment_id
    )
    if run is None:
        raise Conflict("This job is already queued or running for the current minute.")
    return JobRunDetail.of(run)


def _validate(type: ProcessType, schedule: str | None) -> str | None:
    """A schedule is checked here, once, rather than at fire time.

    The alternative is a job that is accepted, looks configured, and then does
    nothing at 03:00 with the reason buried in a worker log.
    """
    if type is ProcessType.CRON:
        if not schedule:
            raise InvalidRequest("A scheduled job needs a cron expression")
        try:
            return parse(schedule).expression
        except InvalidSchedule as exc:
            raise InvalidRequest(str(exc)) from exc
    if schedule:
        raise InvalidRequest(
            f"A {type.value} process runs continuously, so a schedule would "
            "be ignored. Remove it, or make this a cron process."
        )
    return None


def _out(process) -> ProcessOut:
    description = None
    if process.runs_on_a_schedule:
        try:
            description = describe(parse(process.schedule))
        except InvalidSchedule:
            description = None
    return ProcessOut.of(
        process,
        description=description,
        next_run_at=process_engine.next_due(process) if process.enabled else None,
    )
