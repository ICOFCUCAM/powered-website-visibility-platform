"""Deploying, watching a deploy happen, promoting and rolling back."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, status
from fastapi.responses import StreamingResponse

from forge.config import Settings
from forge.deps import Authenticated, ProjectDep, SettingsDep
from forge.domain.errors import Conflict
from forge.domain.models import Deployment, DeploymentTrigger, Project
from forge.engine import promote as promote_engine
from forge.engine import service
from forge.engine.logs import LogWriter
from forge.repositories import deployments as deployment_repo
from forge.repositories import projects as project_repo
from forge.routers.schemas import DeploymentOut, DeployRequest, LogOut

project_router = APIRouter(
    prefix="/api/projects", tags=["deployments"], dependencies=[Authenticated]
)
router = APIRouter(
    prefix="/api/deployments", tags=["deployments"], dependencies=[Authenticated]
)

#: How often the log stream looks for new lines. Fast enough to read like a
#: terminal, slow enough that a watched build is not one query per frame.
STREAM_POLL_SECONDS = 0.5

#: Stop streaming a finished deployment once its log stops growing. Without a
#: bound, a browser tab left open holds a connection and a pool slot forever.
STREAM_IDLE_LIMIT = 6


@project_router.post("/{project_ref}/deploy", status_code=status.HTTP_202_ACCEPTED)
async def deploy(
    project: ProjectDep, body: DeployRequest, settings: SettingsDep
) -> DeploymentOut:
    """Queue a deployment. Returns immediately; the worker does the building."""
    deployment = await service.queue_deploy(
        project,
        ref=body.ref,
        sha=body.sha,
        trigger=DeploymentTrigger.MANUAL,
    )
    return _out(deployment, project, settings)


@project_router.get("/{project_ref}/deployments")
async def list_deployments(
    project: ProjectDep, settings: SettingsDep, limit: int = 50
) -> list[DeploymentOut]:
    return [
        _out(deployment, project, settings)
        for deployment in await deployment_repo.list_for_project(
            project.id, limit=min(limit, 200)
        )
    ]


@router.get("/{short_id}")
async def get_deployment(short_id: str, settings: SettingsDep) -> DeploymentOut:
    deployment = await deployment_repo.get_by_short_id(short_id)
    project = await project_repo.get(deployment.project_id)
    return _out(deployment, project, settings)


@router.get("/{short_id}/logs")
async def get_logs(short_id: str, after: int = 0, limit: int = 2000) -> list[LogOut]:
    deployment = await deployment_repo.get_by_short_id(short_id)
    lines = await deployment_repo.read_logs(
        deployment.id, after=after, limit=min(limit, 5000)
    )
    return [
        LogOut(seq=line.seq, stream=line.stream.value, line=line.line, at=line.at)
        for line in lines
    ]


@router.get("/{short_id}/logs/stream")
async def stream_logs(short_id: str, after: int = 0) -> StreamingResponse:
    """Server-sent events, so a deploy can be watched as it happens.

    Polling the table rather than listening on a Postgres channel: the writer
    batches its inserts anyway, so a notification would arrive in the same
    bursts this poll finds, for the cost of a dedicated connection per viewer.
    """
    deployment = await deployment_repo.get_by_short_id(short_id)

    async def events() -> AsyncIterator[str]:
        cursor = after
        idle = 0
        while True:
            lines = await deployment_repo.read_logs(deployment.id, after=cursor)
            for line in lines:
                cursor = line.seq
                payload = json.dumps(
                    {
                        "seq": line.seq,
                        "stream": line.stream.value,
                        "line": line.line,
                        "at": line.at.isoformat(),
                    }
                )
                yield f"data: {payload}\n\n"

            current = await deployment_repo.get(deployment.id)
            if current.status.is_terminal:
                idle = 0 if lines else idle + 1
                if idle >= STREAM_IDLE_LIMIT:
                    done = json.dumps(
                        {"status": current.status.value, "error": current.error}
                    )
                    yield f"event: done\ndata: {done}\n\n"
                    return
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{short_id}/promote")
async def promote(short_id: str, settings: SettingsDep) -> DeploymentOut:
    """Make this deployment serve production.

    The same call whether it is newer or older than what is serving now — see
    forge.engine.promote for why rollback is not a separate path.
    """
    deployment = await deployment_repo.get_by_short_id(short_id)
    project = await project_repo.get(deployment.project_id)
    log = await LogWriter.resume(deployment.id)
    workdir = settings.build_root / deployment.short_id

    try:
        if project.production_deployment_id == deployment.id:
            await log.system("already serving production — nothing to do")
            return _out(deployment, project, settings)

        promoted = await promote_engine.rollback(
            project, deployment, settings=settings, log=log, workdir=workdir
        )
    finally:
        await log.flush()

    return _out(promoted, await project_repo.get(project.id), settings)


@router.post("/{short_id}/redeploy", status_code=status.HTTP_202_ACCEPTED)
async def redeploy(short_id: str, settings: SettingsDep) -> DeploymentOut:
    """Build this deployment's commit again, as a new deployment.

    Used after changing an environment variable, which only takes effect on a
    fresh build.
    """
    deployment = await deployment_repo.get_by_short_id(short_id)
    project = await project_repo.get(deployment.project_id)
    queued = await service.redeploy(project, deployment)
    return _out(queued, project, settings)


@router.post("/{short_id}/cancel")
async def cancel(short_id: str, settings: SettingsDep) -> DeploymentOut:
    """Cancel a deployment that has not started building.

    Only a queued one. A build already in flight is left alone: killing it
    partway would leave a dangling image layer and a container whose state the
    worker is midway through recording, and the worst case of letting it
    finish is one wasted build.
    """
    deployment = await deployment_repo.get_by_short_id(short_id)
    project = await project_repo.get(deployment.project_id)
    if deployment.status.is_terminal:
        raise Conflict(
            f"Deployment #{deployment.number} has already finished "
            f"({deployment.status.value})"
        )
    if deployment.status.is_in_flight:
        raise Conflict(
            f"Deployment #{deployment.number} is already {deployment.status.value} "
            "and cannot be cancelled. It will not touch production if it fails."
        )
    return _out(await deployment_repo.mark_cancelled(deployment.id), project, settings)


def _out(deployment: Deployment, project: Project, settings: Settings) -> DeploymentOut:
    return DeploymentOut.of(
        deployment,
        url=settings.deployment_url(deployment.short_id),
        is_production=project.production_deployment_id == deployment.id,
    )
