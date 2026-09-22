"""Workers and scheduled jobs.

Both run the deployment's image with a different command, which is the point:
the worker consuming a queue is running exactly the code the website is
running, because it is the same image, not a second build of the same commit.

**Only the production deployment gets them.** A preview of a branch must not
start a second consumer on the same queue, and must not run the nightly
billing job against real data because someone opened a pull request. That rule
is enforced here — `reconcile_workers` is called from promotion and nowhere
else, and a job run resolves its image from the project's production pointer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from forge.adapters import containers
from forge.config import Settings
from forge.domain import naming
from forge.domain.errors import Conflict
from forge.domain.models import (
    Deployment,
    EnvTarget,
    JobRun,
    JobStatus,
    Process,
    Project,
)
from forge.engine import environment
from forge.engine.logs import LogWriter
from forge.repositories import deployments as deployment_repo
from forge.repositories import processes as process_repo
from forge.repositories import projects as project_repo


async def reconcile_workers(
    project: Project,
    deployment: Deployment,
    *,
    settings: Settings,
    log: LogWriter,
    workdir: Path,
) -> int:
    """Bring the project's worker containers onto `deployment`'s image.

    Called after a promotion, so workers roll forward with production and roll
    *back* with it too — a rollback that left the old workers running the new
    code would undo half the change, which is the worst of both.

    Recreated rather than updated, because a container's image cannot be
    changed in place. Every worker is stopped and started again, so a worker
    must be able to survive being killed — which a queue consumer has to be
    able to do anyway.
    """
    processes = await process_repo.list_long_running(project.id)
    wanted: dict[str, Process] = {}
    for process in processes:
        for replica in range(process.replicas):
            name = naming.worker_container_name(project.slug, process.name, replica)
            wanted[name] = process

    existing = set(await containers.list_process_containers(project.slug))

    # Anything running that this deployment does not want: a process that was
    # deleted, disabled, or scaled down.
    for name in sorted(existing - set(wanted)):
        await containers.stop(name)
        await containers.remove(name)
        await log.system(f"stopped worker {name}")

    if not wanted:
        return 0

    if not deployment.image_tag:
        raise Conflict("This deployment has no image, so its workers cannot start")

    env = await _environment(
        project, deployment, settings=settings, target=EnvTarget.PRODUCTION
    )
    env_file = environment.write_runtime_env_file(env, workdir / "worker.env")

    started = 0
    for name, process in sorted(wanted.items()):
        await containers.run_worker(
            containers.TaskSpec(
                image=deployment.image_tag,
                name=name,
                network=settings.network,
                command=process.command,
                memory_mb=process.memory_mb,
                env_file=env_file,
                inline_env=env.inline,
                labels={
                    containers.OWNER_LABEL: containers.OWNER_VALUE,
                    containers.PROJECT_LABEL: project.slug,
                    containers.PROCESS_LABEL: process.name,
                    containers.ROLE_LABEL: "worker",
                    containers.DEPLOYMENT_LABEL: deployment.short_id,
                },
            )
        )
        started += 1

    await log.system(
        f"{started} worker container{'' if started == 1 else 's'} now running "
        f"deployment #{deployment.number}"
    )
    return started


async def run_job(run: JobRun, process: Process, *, settings: Settings) -> JobRun:
    """Execute one claimed cron slot.

    Never raises: a scheduler that dies because a job failed would take every
    other project's jobs with it. Every outcome is recorded on the run.
    """
    project = await project_repo.get(process.project_id)

    deployment = await _production_deployment(project)
    if deployment is None:
        return await process_repo.finish_run(
            run.id,
            status=JobStatus.SKIPPED.value,
            detail=(
                "Nothing is serving production, so there is no image to run "
                "this job from."
            ),
        )

    slot = run.scheduled_for.astimezone(UTC).strftime("%Y%m%dt%H%M")
    name = naming.job_container_name(project.slug, process.name, slot)
    workdir = settings.build_root / "jobs" / str(run.id)

    try:
        env = await _environment(
            project, deployment, settings=settings, target=EnvTarget.PRODUCTION
        )
        env_file = environment.write_runtime_env_file(env, workdir / "job.env")

        result = await containers.run_once(
            containers.TaskSpec(
                image=deployment.image_tag or "",
                name=name,
                network=settings.network,
                command=process.command,
                memory_mb=process.memory_mb,
                env_file=env_file,
                inline_env=env.inline,
                labels={
                    containers.OWNER_LABEL: containers.OWNER_VALUE,
                    containers.PROJECT_LABEL: project.slug,
                    containers.PROCESS_LABEL: process.name,
                    containers.ROLE_LABEL: "cron",
                    containers.DEPLOYMENT_LABEL: deployment.short_id,
                },
            ),
            timeout=process.timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - the scheduler must never die
        return await process_repo.finish_run(
            run.id,
            status=JobStatus.FAILED.value,
            detail=f"{type(exc).__name__}: {exc}",
        )
    finally:
        _remove_quietly(workdir)

    if result.timed_out:
        return await process_repo.finish_run(
            run.id,
            status=JobStatus.TIMED_OUT.value,
            detail=(
                f"Killed after {process.timeout_seconds}s. Raise the timeout, "
                "or make the job finish sooner."
            ),
            output=result.output,
        )

    return await process_repo.finish_run(
        run.id,
        status=(
            JobStatus.SUCCEEDED.value if result.succeeded else JobStatus.FAILED.value
        ),
        exit_code=result.exit_code,
        detail=None if result.succeeded else f"Exited {result.exit_code}",
        output=result.output,
    )


async def _production_deployment(project: Project) -> Deployment | None:
    if project.production_deployment_id is None:
        return None
    deployment = await deployment_repo.get(project.production_deployment_id)
    return deployment if deployment.image_tag else None


async def _environment(
    project: Project,
    deployment: Deployment,
    *,
    settings: Settings,
    target: EnvTarget,
):
    return await environment.collect(
        project.id,
        target=target,
        key=settings.master_key,
        injected=environment.platform_variables(
            short_id=deployment.short_id,
            git_sha=deployment.git_sha,
            url=settings.deployment_url(deployment.short_id),
            target=target,
        ),
    )


def _remove_quietly(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def next_due(process: Process, *, now: datetime | None = None) -> datetime | None:
    """When this process will next run, for display only."""
    from forge.domain.schedule import InvalidSchedule, parse

    if not process.runs_on_a_schedule:
        return None
    try:
        schedule = parse(process.schedule or "")
    except InvalidSchedule:
        return None
    return schedule.next_slot(now or datetime.now(UTC))
