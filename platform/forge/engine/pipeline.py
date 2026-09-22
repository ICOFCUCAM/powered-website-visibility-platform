"""One deployment, from a git reference to a container serving traffic.

The stages are clone, detect, build, launch, promote. Each one either advances
the deployment's status or fails it with a sentence that says what to do next,
and every stage writes to the deployment log as it goes, so a deploy that is
taking too long can be watched rather than guessed at.

Nothing here retries. A failed deploy leaves the previous one serving —
production never moved, because promotion is the last step and only runs after
the new container has answered an HTTP request. Retrying is the owner's
decision, and it is one button.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from forge.adapters import containers, source
from forge.config import Settings
from forge.domain import naming
from forge.domain.detect import Overrides, detect
from forge.domain.errors import ForgeError
from forge.domain.models import Deployment, EnvTarget, LogStream, Project
from forge.engine import environment, promote
from forge.engine.launch import launch
from forge.engine.logs import LogWriter
from forge.repositories import deployments as deployment_repo
from forge.repositories import projects as project_repo

#: Written into a build context that has no .dockerignore of its own.
#:
#: `node_modules` is the one that matters. A developer's local copy is built
#: for their machine's libc and architecture, it is often larger than the rest
#: of the repository put together, and the image is about to install its own
#: anyway — so copying it in is pure cost, and occasionally a broken build
#: that works locally.
DEFAULT_DOCKERIGNORE = """\
.git
node_modules
**/node_modules
.next/cache
.venv
__pycache__
*.pyc
.env
.env.*
"""


async def run_deployment(deployment: Deployment, *, settings: Settings) -> Deployment:
    """Execute a claimed deployment. Never raises; failures are recorded."""
    project = await project_repo.get(deployment.project_id)
    log = await LogWriter.resume(deployment.id)
    workdir = settings.build_root / deployment.short_id
    is_production = deployment.git_ref == project.production_branch
    target = EnvTarget.PRODUCTION if is_production else EnvTarget.PREVIEW

    try:
        await log.system(
            f"deploying {project.name} #{deployment.number} — "
            f"{deployment.git_ref} at {deployment.git_sha[:8]} "
            f"({deployment.trigger.value})"
        )

        app_dir = await _checkout(project, deployment, workdir=workdir, log=log)
        await deployment_repo.heartbeat(deployment.id)

        plan = await _plan(project, app_dir, log=log)
        await deployment_repo.heartbeat(deployment.id)

        image_tag = naming.image_tag(project.slug, deployment.git_sha)
        await _build(
            project,
            deployment,
            plan=plan,
            app_dir=app_dir,
            image_tag=image_tag,
            target=target,
            settings=settings,
            log=log,
        )

        deployment = await deployment_repo.mark_built(
            deployment.id,
            image_tag=image_tag,
            framework=plan.framework,
            internal_port=plan.port,
        )

        container_id = await launch(
            project,
            deployment,
            settings=settings,
            log=log,
            target=target,
            workdir=workdir,
        )
        deployment = await deployment_repo.mark_ready(
            deployment.id, container_id=container_id
        )

        url = settings.deployment_url(deployment.short_id)
        await log.system(f"ready at {url}")

        if is_production:
            await promote.promote(
                project, deployment, settings=settings, log=log, workdir=workdir
            )
        else:
            await log.system(
                f"{deployment.git_ref} is not the production branch "
                f"({project.production_branch}) — this is a preview and "
                "production was not touched"
            )
        return deployment

    except ForgeError as exc:
        await log.system(f"failed: {exc.message}")
        return await deployment_repo.mark_failed(deployment.id, error=exc.message)

    except Exception as exc:  # noqa: BLE001 - the worker must never die
        # An unexpected exception here would otherwise strand the deployment
        # in `building` until the lease expires, and take the worker with it.
        await log.system(f"failed unexpectedly: {type(exc).__name__}: {exc}")
        return await deployment_repo.mark_failed(
            deployment.id, error=f"{type(exc).__name__}: {exc}"
        )

    finally:
        await log.flush()
        # The build context holds a full checkout and the plaintext env files.
        # Neither has any reason to outlive the deploy.
        shutil.rmtree(workdir, ignore_errors=True)


async def _checkout(
    project: Project, deployment: Deployment, *, workdir: Path, log: LogWriter
) -> Path:
    await log.system(f"cloning {project.repo_url} at {deployment.git_sha[:8]}")
    repo_dir = workdir / "repo"
    await source.fetch(
        project.repo_url,
        deployment.git_ref,
        repo_dir,
        sha=deployment.git_sha,
        log=log.sink(LogStream.BUILD),
    )

    app_dir = repo_dir
    if project.root_directory:
        app_dir = repo_dir / project.root_directory
        if not app_dir.is_dir():
            raise ForgeError(
                f"The project's root directory {project.root_directory!r} "
                "does not exist in this commit."
            )
        await log.system(f"building from {project.root_directory}/")
    return app_dir


async def _plan(project: Project, app_dir: Path, *, log: LogWriter):
    plan = detect(
        app_dir,
        Overrides(
            framework=project.framework,
            install_command=project.install_command,
            build_command=project.build_command,
            start_command=project.start_command,
            port=project.port,
        ),
    )
    await log.system(plan.reason)
    for note in plan.notes:
        await log.system(f"note: {note}")
    return plan


async def _build(
    project: Project,
    deployment: Deployment,
    *,
    plan,
    app_dir: Path,
    image_tag: str,
    target: EnvTarget,
    settings: Settings,
    log: LogWriter,
) -> None:
    dockerfile = app_dir / "Dockerfile.forge"
    dockerfile.write_text(plan.dockerfile)
    for relative, content in plan.context_files:
        (app_dir / relative).write_text(content)

    if not (app_dir / ".dockerignore").exists():
        (app_dir / ".dockerignore").write_text(DEFAULT_DOCKERIGNORE)

    env = await environment.collect(
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
    secret_file = environment.write_build_secret(
        env, settings.build_root / deployment.short_id / "build.env"
    )

    await log.system(f"building image {image_tag}")
    await containers.build(
        context=app_dir,
        dockerfile=dockerfile,
        tag=image_tag,
        secret_env_file=secret_file,
        log=log.sink(LogStream.BUILD),
        timeout=settings.build_timeout_seconds,
    )
    await log.system("image built")
