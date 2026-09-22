"""Operations the API, the webhook and the CLI all need.

Queueing a deploy is the same act whether a push, a person or a script asked
for it, and only the recorded trigger differs. Keeping that in one place is
what stops the webhook and the CLI drifting into two subtly different ideas of
what a deployment is.
"""

from __future__ import annotations

from uuid import UUID

from forge.adapters import containers, source
from forge.config import Settings
from forge.domain import naming
from forge.domain.models import Deployment, DeploymentTrigger, Project
from forge.repositories import deployments as deployment_repo
from forge.repositories import projects as project_repo


async def queue_deploy(
    project: Project,
    *,
    ref: str | None = None,
    sha: str | None = None,
    trigger: DeploymentTrigger = DeploymentTrigger.MANUAL,
    message: str | None = None,
    author: str | None = None,
    rolled_back_from: UUID | None = None,
) -> Deployment:
    """Create a queued deployment, resolving the branch to a commit first.

    Resolving up front is what makes the history honest. If the sha were
    resolved by the worker instead, two deploys queued a minute apart would
    both say "main" and there would be no record of which commit each one
    actually built.
    """
    reference = ref or project.production_branch
    commit = sha or await source.resolve_head(project.repo_url, reference)

    return await deployment_repo.create(
        project_id=project.id,
        short_id=naming.deployment_short_id(project.slug),
        git_sha=commit,
        git_ref=reference,
        trigger=trigger,
        git_message=message,
        git_author=author,
        rolled_back_from=rolled_back_from,
    )


async def redeploy(project: Project, deployment: Deployment) -> Deployment:
    """Build the same commit again.

    The common reason is a changed environment variable: variables are read at
    build time as well as run time, so changing one only takes effect on a new
    build. The alternative — restarting the container — would pick up the new
    runtime value and keep the old baked-in one, which is the worse kind of
    half-applied change.
    """
    return await queue_deploy(
        project,
        ref=deployment.git_ref,
        sha=deployment.git_sha,
        trigger=DeploymentTrigger.REDEPLOY,
        message=deployment.git_message,
        author=deployment.git_author,
    )


async def reconcile(settings: Settings) -> dict[str, int]:
    """Make the database agree with the Docker daemon.

    Run at worker startup. The two drift for ordinary reasons — the host
    rebooted, a container was OOM-killed, someone ran `docker rm` — and the
    cost of not noticing is a project whose production deployment is listed as
    serving while its domain returns 502.

    Docker is treated as the truth about containers and the database as the
    truth about intent, so a container that is gone is recorded as gone, and a
    deployment whose worker died is failed rather than left building forever.
    """
    await containers.ensure_network(settings.network)

    detached = 0
    restored = 0
    for project in await project_repo.list_all():
        for deployment in await deployment_repo.list_for_project(project.id):
            if deployment.container_id is None:
                continue
            if await containers.is_running(deployment.container_id):
                continue
            await deployment_repo.set_container(deployment.id, None)
            detached += 1
            if project.production_deployment_id == deployment.id:
                # Production pointing at a dead container is the one case worth
                # acting on rather than just recording: the site is down.
                restored += 1

    abandoned = await deployment_repo.reclaim_abandoned(
        older_than_seconds=settings.build_timeout_seconds + 120
    )
    return {
        "detached": detached,
        "production_down": restored,
        "abandoned": len(abandoned),
    }
