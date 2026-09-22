"""Starting a deployment's container and proving it serves.

Shared by the deploy pipeline and by promotion, because they need exactly the
same thing for different reasons: the pipeline has just built an image, and
promotion has found an old deployment whose container was reclaimed. Both end
with "a container for this image, on the network, answering HTTP".
"""

from __future__ import annotations

from pathlib import Path

from forge.adapters import containers
from forge.config import Settings
from forge.domain import naming
from forge.domain.buildplan import DEFAULT_PORT
from forge.domain.errors import Conflict, DeployFailed
from forge.domain.models import Deployment, EnvTarget, LogStream, Project
from forge.engine import environment, health
from forge.engine.logs import LogWriter
from forge.repositories import deployments as deployment_repo


async def launch(
    project: Project,
    deployment: Deployment,
    *,
    settings: Settings,
    log: LogWriter,
    target: EnvTarget,
    workdir: Path,
) -> str:
    """Run the deployment's image and return the container id once healthy.

    On any failure the container is destroyed before the error is raised. A
    half-started deployment left behind would hold its name, its memory and —
    because its Traefik labels are already in place — a route to a process
    that does not answer.
    """
    if not deployment.image_tag:
        raise Conflict("This deployment has no image to run")
    if not await containers.image_exists(deployment.image_tag):
        raise Conflict(
            f"The image for deployment #{deployment.number} is no longer on "
            "this host, so it cannot be started without rebuilding. "
            "Redeploy the commit instead."
        )

    port = deployment.internal_port or DEFAULT_PORT
    url = settings.deployment_url(deployment.short_id)

    env = await environment.collect(
        project.id,
        target=target,
        key=settings.master_key,
        injected=environment.platform_variables(
            short_id=deployment.short_id,
            git_sha=deployment.git_sha,
            url=url,
            target=target,
        ),
    )
    env_file = environment.write_runtime_env_file(env, workdir / "runtime.env")

    name = naming.container_name(deployment.short_id)
    # A previous attempt that died after `docker run` but before the database
    # write leaves this name taken; without clearing it the retry fails on a
    # name conflict that reads like a Docker problem rather than a stale one.
    await containers.remove_by_name(name)

    await log.system(
        f"starting container on port {port} with {len(env)} environment "
        f"variable{'' if len(env) == 1 else 's'}"
    )

    spec = containers.RunSpec(
        image=deployment.image_tag,
        name=name,
        network=settings.network,
        host=settings.deployment_host(deployment.short_id),
        port=port,
        router=naming.router_id(deployment.short_id),
        memory_mb=project.memory_mb,
        cpu_shares=project.cpu_shares,
        cert_resolver=settings.cert_resolver,
        env_file=env_file,
        inline_env=env.inline,
        labels={
            containers.PROJECT_LABEL: project.slug,
            containers.DEPLOYMENT_LABEL: deployment.short_id,
        },
    )

    container_id = await containers.run(spec, log=log.sink(LogStream.SYSTEM))

    result = await health.wait_until_healthy(
        container_id=container_id,
        network=settings.network,
        port=port,
        path=settings.health_path,
        timeout=settings.health_timeout_seconds,
    )

    if not result.healthy:
        # The container's own output is the only thing that explains this, and
        # it is about to be destroyed, so it is copied into the deploy log
        # first. Without this the failure reads "unhealthy" and nothing else.
        tail = await containers.logs(container_id, tail=100)
        await log.system(f"health check failed: {result.detail}")
        await log.system("--- last 100 lines from the container ---")
        for line in tail.splitlines():
            await log.write(line, stream=LogStream.RUN)
        await containers.remove(container_id, force=True)
        await log.flush()
        raise DeployFailed(f"The container never became healthy: {result.detail}")

    await log.system(
        f"healthy after {result.elapsed_seconds:.1f}s "
        f"({result.attempts} attempt{'' if result.attempts == 1 else 's'}) — "
        f"{result.detail}"
    )
    await deployment_repo.set_container(deployment.id, container_id)
    return container_id


async def ensure_serving(
    project: Project,
    deployment: Deployment,
    *,
    settings: Settings,
    log: LogWriter,
    workdir: Path,
) -> str:
    """The container for this deployment, started if it is not already.

    Rollback goes through here. A deployment kept warm answers instantly; one
    whose container was reclaimed to free memory is restarted from its image,
    which costs seconds rather than a rebuild.
    """
    if deployment.container_id and await containers.is_running(deployment.container_id):
        return deployment.container_id

    await log.system(
        f"deployment #{deployment.number} is not resident — restarting it from its image"
    )
    return await launch(
        project,
        deployment,
        settings=settings,
        log=log,
        target=EnvTarget.PRODUCTION,
        workdir=workdir,
    )
