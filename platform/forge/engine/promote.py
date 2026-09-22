"""Moving production from one deployment to another.

Promotion and rollback are the same operation. There is no separate rollback
path, no "undo", and nothing that reverses a previous change — rolling back is
promoting a deployment that happens to be older. That equivalence is the
reason rollback is trustworthy: it is the code path that runs on every
successful deploy, not an emergency path exercised only during emergencies.
"""

from __future__ import annotations

from pathlib import Path

from forge.adapters import containers
from forge.config import Settings
from forge.domain.errors import Conflict
from forge.domain.models import Deployment, Project
from forge.engine import processes, routing
from forge.engine.launch import ensure_serving
from forge.engine.logs import LogWriter
from forge.repositories import deployments as deployment_repo
from forge.repositories import projects as project_repo


async def promote(
    project: Project,
    deployment: Deployment,
    *,
    settings: Settings,
    log: LogWriter,
    workdir: Path,
) -> Deployment:
    """Point the project's production domains at `deployment`.

    Order matters and is deliberate: the container is proven to serve *before*
    anything routes to it, and the database pointer moves *after* the router
    file is written. A crash between the two leaves production serving
    correctly from a deployment the database has not yet caught up to, which
    reconciliation repairs. The other order would leave the database claiming a
    promotion that never reached the router.
    """
    deployment = await deployment_repo.ensure_promotable(deployment.id, project.id)
    await ensure_serving(project, deployment, settings=settings, log=log, workdir=workdir)

    await log.system(await routing.publish(project, deployment, settings=settings))
    await project_repo.set_production(project.id, deployment.id)
    await log.system(f"deployment #{deployment.number} is now serving production")

    # Workers roll forward with production — and back with it. A rollback that
    # left the old workers running the new code would undo half the change,
    # which is worse than either version on its own. Failing to start them is
    # not allowed to un-promote a healthy website, so it is reported rather
    # than raised: the site is up, and the worker is the thing to go and fix.
    project = await project_repo.get(project.id)
    try:
        await processes.reconcile_workers(
            project, deployment, settings=settings, log=log, workdir=workdir
        )
    except Exception as exc:  # noqa: BLE001 - reported, never fatal to a promotion
        await log.system(
            f"WARNING: production is serving, but the workers did not start: {exc}"
        )

    await reclaim(project, protect={deployment.id}, log=log)
    return deployment


async def rollback(
    project: Project,
    deployment: Deployment,
    *,
    settings: Settings,
    log: LogWriter,
    workdir: Path,
) -> Deployment:
    if project.production_deployment_id == deployment.id:
        raise Conflict(f"Deployment #{deployment.number} is already serving production")
    await log.system(
        f"rolling production back to deployment #{deployment.number} "
        f"({deployment.git_sha[:8]})"
    )
    return await promote(project, deployment, settings=settings, log=log, workdir=workdir)


async def reclaim(
    project: Project,
    *,
    protect: set,
    log: LogWriter | None = None,
) -> int:
    """Stop containers for superseded deployments, keeping the newest few warm.

    The images stay. What is freed is memory, which on a single host is the
    resource that actually runs out — twenty projects with six live
    deployments each is a hundred and twenty idle containers holding RAM for
    URLs nobody has opened in weeks. A reclaimed deployment is still READY and
    one restart away from serving.
    """
    protected = set(protect)
    if project.production_deployment_id:
        protected.add(project.production_deployment_id)

    stale = await deployment_repo.reclaimable(
        project.id, keep=project.keep_warm, protect=protected
    )
    for deployment in stale:
        if not deployment.container_id:
            continue
        await containers.stop(deployment.container_id)
        await containers.remove(deployment.container_id)
        await deployment_repo.set_container(deployment.id, None)
        if log:
            await log.system(
                f"reclaimed deployment #{deployment.number} "
                "(still rollback-able from its image)"
            )
    return len(stale)
