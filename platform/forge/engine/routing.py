"""Publishing a project's production routing.

One function decides which domains are routed and where they point, because
three callers need that decision — a promotion, a rollback, and any change to
the domain list — and three copies of it would drift until one of them started
routing an unverified hostname.
"""

from __future__ import annotations

from forge.adapters import edge
from forge.config import Settings
from forge.domain.models import Deployment, Project
from forge.repositories import projects as project_repo


async def publish(project: Project, deployment: Deployment, *, settings: Settings) -> str:
    """Point the project's verified domains at `deployment`. Returns a
    sentence describing what was done, for the deploy log.

    Unverified domains are deliberately excluded. Traefik asks for a
    certificate as soon as a hostname appears in its configuration, and a
    hostname whose DNS does not point here fails that challenge — against a
    rate limit shared by every site on the host.
    """
    verified = [d for d in await project_repo.list_domains(project.id) if d.is_verified]

    if not verified:
        edge.clear_route(project.slug, directory=settings.router_config_dir)
        return (
            "no verified custom domains — serving on "
            f"{settings.deployment_url(deployment.short_id)}"
        )

    primary = next((d for d in verified if d.is_primary), verified[0])
    aliases = tuple(d.host for d in verified if d.host != primary.host)

    edge.write_route(
        edge.ProductionRoute(
            project_slug=project.slug,
            service=deployment.short_id,
            primary_host=primary.host,
            aliases=aliases,
        ),
        directory=settings.router_config_dir,
        cert_resolver=settings.cert_resolver,
    )

    described = primary.host
    if aliases:
        described += f" (+{len(aliases)} redirecting to it)"
    return f"production routed to {described}"


async def refresh(project: Project, *, settings: Settings) -> str:
    """Re-publish after the domain list changed, if anything is serving.

    Before a project's first successful deploy there is no service for a
    router to name, and writing one anyway makes Traefik log an unresolvable
    reference on every reload.
    """
    if project.production_deployment_id is None:
        edge.clear_route(project.slug, directory=settings.router_config_dir)
        return "no production deployment yet — routing not published"

    from forge.repositories import deployments as deployment_repo

    deployment = await deployment_repo.get(project.production_deployment_id)
    return await publish(project, deployment, settings=settings)
