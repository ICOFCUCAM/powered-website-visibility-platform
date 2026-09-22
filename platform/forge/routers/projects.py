"""Projects, their environment variables and their domains."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from forge.adapters import crypto, edge
from forge.deps import Authenticated, ProjectDep, SettingsDep
from forge.domain import naming
from forge.domain.errors import InvalidRequest, NotFound
from forge.domain.models import EnvTarget
from forge.domain.repo_url import validate_repo_url
from forge.engine import routing, verify
from forge.repositories import projects as project_repo
from forge.routers.schemas import (
    AddDomain,
    CreateProject,
    DomainOut,
    EnvOut,
    ProjectOut,
    SetEnv,
    UpdateProject,
)

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Authenticated])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(body: CreateProject) -> ProjectOut:
    validate_repo_url(body.repo_url)
    project = await project_repo.create(
        slug=body.slug or naming.slugify(body.name),
        name=body.name,
        repo_url=body.repo_url.strip(),
        production_branch=body.production_branch,
        root_directory=body.root_directory,
        framework=body.framework,
        install_command=body.install_command,
        build_command=body.build_command,
        start_command=body.start_command,
        port=body.port,
        memory_mb=body.memory_mb,
        cpu_shares=body.cpu_shares,
    )
    return ProjectOut.of(project)


@router.get("")
async def list_projects() -> list[ProjectOut]:
    return [ProjectOut.of(project) for project in await project_repo.list_all()]


@router.get("/{project_ref}")
async def get_project(project: ProjectDep) -> ProjectOut:
    return ProjectOut.of(project)


@router.patch("/{project_ref}")
async def update_project(project: ProjectDep, body: UpdateProject) -> ProjectOut:
    changes = body.changes()
    if "repo_url" in changes:
        validate_repo_url(changes["repo_url"])
    return ProjectOut.of(await project_repo.update(project.id, changes))


@router.delete("/{project_ref}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project: ProjectDep, settings: SettingsDep) -> Response:
    """Remove the project, its deployments and its routing.

    Containers are not stopped here. They carry the project label, so the
    worker's next reconciliation finds them without an owning row and cleans
    them up — which keeps a slow `docker stop` out of a request that the
    caller is waiting on.
    """
    edge.clear_route(project.slug, directory=settings.router_config_dir)
    await project_repo.delete(project.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------


@router.get("/{project_ref}/env")
async def list_env(project: ProjectDep) -> list[EnvOut]:
    return [
        EnvOut(key=var.key, target=var.target.value, updated_at=var.updated_at)
        for var in await project_repo.list_env(project.id)
    ]


@router.put("/{project_ref}/env")
async def set_env(project: ProjectDep, body: SetEnv, settings: SettingsDep) -> EnvOut:
    """Store a variable, encrypted.

    Takes effect on the next build, not immediately. Variables are read at
    build time as well as at run time — a bundler inlines them — so applying
    one to a running container would give it a value its own bundle disagrees
    with.
    """
    var = await project_repo.set_env(
        project.id,
        body.key,
        crypto.encrypt(body.value, key=settings.master_key),
        body.target,
    )
    return EnvOut(key=var.key, target=var.target.value, updated_at=var.updated_at)


@router.delete("/{project_ref}/env/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_env(
    project: ProjectDep, key: str, target: EnvTarget | None = None
) -> Response:
    removed = await project_repo.delete_env(project.id, key, target)
    if not removed:
        raise NotFound(f"No variable named {key!r} on this project")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------


@router.get("/{project_ref}/domains")
async def list_domains(project: ProjectDep) -> list[DomainOut]:
    return [DomainOut.of(d) for d in await project_repo.list_domains(project.id)]


@router.post("/{project_ref}/domains", status_code=status.HTTP_201_CREATED)
async def add_domain(
    project: ProjectDep, body: AddDomain, settings: SettingsDep
) -> DomainOut:
    if body.host.endswith(settings.deploy_domain):
        raise InvalidRequest(
            f"{body.host} is under the platform's own domain "
            f"({settings.deploy_domain}), which every deployment already uses. "
            "Add a domain you control instead."
        )
    return DomainOut.of(
        await project_repo.add_domain(project.id, body.host, primary=body.primary)
    )


@router.post("/{project_ref}/domains/{host}/verify")
async def verify_domain(
    project: ProjectDep, host: str, settings: SettingsDep
) -> dict[str, object]:
    """Check DNS, and route production here if it passes.

    Verification is explicit rather than automatic on a timer, because the
    person adding the domain is the one who just changed the DNS and is the
    only one who knows to expect it to work.
    """
    domains = {d.host: d for d in await project_repo.list_domains(project.id)}
    domain = domains.get(host.strip().lower())
    if domain is None:
        raise NotFound(f"{host} is not attached to this project")

    result = await verify.verify(domain.host, expected_host=settings.deploy_domain)
    if not result.verified:
        return {"verified": False, "detail": result.detail}

    await project_repo.mark_domain_verified(domain.id)
    await routing.refresh(await project_repo.get(project.id), settings=settings)
    return {"verified": True, "detail": result.detail}


@router.delete("/{project_ref}/domains/{host}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_domain(
    project: ProjectDep, host: str, settings: SettingsDep
) -> Response:
    removed = await project_repo.remove_domain(project.id, host)
    if not removed:
        raise NotFound(f"{host} is not attached to this project")
    await routing.refresh(await project_repo.get(project.id), settings=settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
