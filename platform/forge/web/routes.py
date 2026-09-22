"""The dashboard.

Server-rendered, and deliberately so. This is the page you open when a deploy
has gone wrong, so it has no build step, no separate deployment and no
JavaScript it needs in order to work — every action on it is a plain form that
posts and redirects. The only script on the page adds live log lines, and the
page is complete without it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from forge.adapters import crypto
from forge.config import Settings, get_settings
from forge.deps import SettingsDep, token_matches
from forge.domain import naming, session
from forge.domain.errors import ForgeError
from forge.domain.models import (
    Deployment,
    Domain,
    EnvTarget,
    EnvVar,
    ProcessType,
    Project,
)
from forge.domain.repo_url import validate_repo_url
from forge.domain.schedule import InvalidSchedule, describe, parse
from forge.engine import processes as process_engine
from forge.engine import promote as promote_engine
from forge.engine import routing, service, verify
from forge.engine.logs import LogWriter
from forge.repositories import deployments as deployment_repo
from forge.repositories import processes as process_repo
from forge.repositories import projects as project_repo

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

router = APIRouter(include_in_schema=False)


class NeedsLogin(Exception):
    """Raised instead of 401 so a browser gets the sign-in page, not JSON."""


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    project: Project
    live: Deployment | None
    live_url: str
    primary_domain: str | None


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def current_session(request: Request, settings: Settings) -> None:
    cookie = request.cookies.get(session.COOKIE_NAME)
    if not cookie:
        raise NeedsLogin
    try:
        session.verify(cookie, token=settings.api_token)
    except session.InvalidSession as exc:
        raise NeedsLogin from exc


def signed_in(request: Request) -> None:
    current_session(request, get_settings())


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, settings: SettingsDep, err: str = ""):
    return _render(request, "login.html", {"err": err}, nav=False)


@router.post("/login")
async def login(
    request: Request,
    settings: SettingsDep,
    token: Annotated[str, Form()],
):
    if not token_matches(token.strip(), settings.api_token):
        return _redirect("/login", err="That token is not valid.")

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        session.COOKIE_NAME,
        session.issue(token=settings.api_token),
        max_age=session.MAX_AGE_SECONDS,
        httponly=True,
        # Lax rather than Strict: Strict would drop the cookie when arriving
        # from a link in a deploy notification, which is exactly the journey
        # this dashboard is opened by.
        samesite="lax",
        secure=settings.tls_enabled,
        path="/",
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(session.COOKIE_NAME, path="/")
    return response


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, settings: SettingsDep, ok: str = "", err: str = ""):
    signed_in(request)
    summaries = []
    for project in await project_repo.list_all():
        summaries.append(await _summarise(project, settings))
    return _render(
        request, "projects.html", {"projects": summaries, "ok": ok, "err": err}
    )


@router.get("/projects/new", response_class=HTMLResponse)
async def new_project_form(request: Request, err: str = ""):
    signed_in(request)
    return _render(request, "new_project.html", {"err": err})


@router.post("/projects/new")
async def create_project(
    request: Request,
    name: Annotated[str, Form()],
    repo_url: Annotated[str, Form()],
    production_branch: Annotated[str, Form()] = "main",
    root_directory: Annotated[str, Form()] = "",
    slug: Annotated[str, Form()] = "",
    memory_mb: Annotated[str, Form()] = "512",
):
    signed_in(request)
    try:
        validate_repo_url(repo_url)
        project = await project_repo.create(
            slug=slug.strip() or naming.slugify(name),
            name=name.strip(),
            repo_url=repo_url.strip(),
            production_branch=production_branch.strip() or "main",
            root_directory=root_directory.strip(),
            memory_mb=_int(memory_mb, 512),
        )
    except ForgeError as exc:
        return _redirect("/projects/new", err=exc.message)
    return _redirect(f"/projects/{project.slug}", ok=f"Created {project.name}.")


@router.get("/projects/{slug}", response_class=HTMLResponse)
async def project_page(
    request: Request, slug: str, settings: SettingsDep, ok: str = "", err: str = ""
):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    summary = await _summarise(project, settings)
    domains = await project_repo.list_domains(project.id)

    return _render(
        request,
        "project.html",
        {
            "project": project,
            "live": summary.live,
            "live_url": summary.live_url,
            "primary_domain": summary.primary_domain,
            "deployments": await deployment_repo.list_for_project(project.id, limit=25),
            "env_vars": await project_repo.list_env(project.id),
            "processes": [
                await _decorate(process)
                for process in await process_repo.list_for_project(project.id)
            ],
            "domains": domains,
            "deploy_domain": settings.deploy_domain,
            "webhook_url": (
                f"{settings.scheme}://forge.{settings.deploy_domain}"
                f"/webhooks/{project.slug}"
            ),
            "ok": ok,
            "err": err,
        },
    )


@router.post("/projects/{slug}/deploy")
async def trigger_deploy(request: Request, slug: str):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    try:
        deployment = await service.queue_deploy(project)
    except ForgeError as exc:
        return _redirect(f"/projects/{slug}", err=exc.message)
    return _redirect(f"/deployments/{deployment.short_id}")


@router.post("/projects/{slug}/settings")
async def save_settings(
    request: Request,
    slug: str,
    name: Annotated[str, Form()] = "",
    production_branch: Annotated[str, Form()] = "",
    root_directory: Annotated[str, Form()] = "",
    framework: Annotated[str, Form()] = "",
    build_command: Annotated[str, Form()] = "",
    start_command: Annotated[str, Form()] = "",
    memory_mb: Annotated[str, Form()] = "",
    keep_warm: Annotated[str, Form()] = "",
):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    changes: dict = {
        "name": name.strip() or project.name,
        "production_branch": production_branch.strip() or project.production_branch,
        "root_directory": root_directory.strip(),
        "memory_mb": _int(memory_mb, project.memory_mb),
        "keep_warm": _int(keep_warm, project.keep_warm),
    }
    # An empty override means "go back to detecting it", which is a real
    # setting and not a missing field — so these are written as NULL rather
    # than skipped the way the blank-but-required fields above are.
    for field, value in (
        ("framework", framework),
        ("build_command", build_command),
        ("start_command", start_command),
    ):
        changes[field] = value.strip() or None

    await project_repo.update(project.id, changes)
    return _redirect(
        f"/projects/{slug}", ok="Settings saved — they apply to the next deploy."
    )


@router.post("/projects/{slug}/delete")
async def delete_project(request: Request, slug: str, settings: SettingsDep):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    from forge.adapters import edge

    edge.clear_route(project.slug, directory=settings.router_config_dir)
    await project_repo.delete(project.id)
    return _redirect("/", ok=f"Deleted {project.name}.")


# ---------------------------------------------------------------------------
# Environment variables and domains
# ---------------------------------------------------------------------------


@router.post("/projects/{slug}/env")
async def add_env(
    request: Request,
    slug: str,
    settings: SettingsDep,
    key: Annotated[str, Form()],
    value: Annotated[str, Form()],
    target: Annotated[str, Form()] = "all",
):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    cleaned = key.strip()
    if not cleaned.replace("_", "").isalnum() or cleaned[:1].isdigit():
        return _redirect(
            f"/projects/{slug}",
            err=f"{cleaned!r} is not a usable variable name.",
        )
    await project_repo.set_env(
        project.id,
        cleaned,
        crypto.encrypt(value, key=settings.master_key),
        EnvTarget(target),
    )
    return _redirect(
        f"/projects/{slug}", ok=f"Saved {cleaned} — it applies on the next deploy."
    )


@router.post("/projects/{slug}/env/{key}/delete")
async def remove_env(request: Request, slug: str, key: str):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    await project_repo.delete_env(project.id, key, None)
    return _redirect(f"/projects/{slug}", ok=f"Removed {key}.")


@router.post("/projects/{slug}/domains")
async def add_domain(
    request: Request,
    slug: str,
    settings: SettingsDep,
    host: Annotated[str, Form()],
    primary: Annotated[str, Form()] = "",
):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    cleaned = host.strip().lower().rstrip(".")
    if "." not in cleaned or "/" in cleaned:
        return _redirect(f"/projects/{slug}", err=f"{host!r} is not a hostname.")
    if cleaned.endswith(settings.deploy_domain):
        return _redirect(
            f"/projects/{slug}",
            err=(
                f"{cleaned} is under the platform's own domain, which every "
                "deployment already uses."
            ),
        )
    try:
        await project_repo.add_domain(project.id, cleaned, primary=bool(primary))
    except ForgeError as exc:
        return _redirect(f"/projects/{slug}", err=exc.message)
    return _redirect(
        f"/projects/{slug}",
        ok=f"Added {cleaned}. Point its DNS here, then verify it.",
    )


@router.post("/projects/{slug}/domains/{host}/verify")
async def verify_domain(request: Request, slug: str, host: str, settings: SettingsDep):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    domains = {d.host: d for d in await project_repo.list_domains(project.id)}
    domain = domains.get(host.lower())
    if domain is None:
        return _redirect(f"/projects/{slug}", err=f"{host} is not on this project.")

    result = await verify.verify(domain.host, expected_host=settings.deploy_domain)
    if not result.verified:
        return _redirect(f"/projects/{slug}", err=result.detail)

    await project_repo.mark_domain_verified(domain.id)
    await routing.refresh(await project_repo.get(project.id), settings=settings)
    return _redirect(f"/projects/{slug}", ok=result.detail)


@router.post("/projects/{slug}/domains/{host}/delete")
async def remove_domain(request: Request, slug: str, host: str, settings: SettingsDep):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    await project_repo.remove_domain(project.id, host)
    await routing.refresh(await project_repo.get(project.id), settings=settings)
    return _redirect(f"/projects/{slug}", ok=f"Removed {host}.")


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


@router.post("/projects/{slug}/processes")
async def add_process(
    request: Request,
    slug: str,
    name: Annotated[str, Form()],
    type: Annotated[str, Form()],
    command: Annotated[str, Form()] = "",
    schedule: Annotated[str, Form()] = "",
    memory_mb: Annotated[str, Form()] = "512",
    replicas: Annotated[str, Form()] = "1",
    timeout_seconds: Annotated[str, Form()] = "900",
):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    kind = ProcessType(type)

    cleaned_schedule: str | None = None
    if kind is ProcessType.CRON:
        try:
            cleaned_schedule = parse(schedule).expression
        except InvalidSchedule as exc:
            return _redirect(f"/projects/{slug}", err=str(exc))

    try:
        await process_repo.create(
            project_id=project.id,
            name=name.strip(),
            type=kind,
            command=command.strip() or None,
            schedule=cleaned_schedule,
            memory_mb=_int(memory_mb, 512),
            replicas=_int(replicas, 1),
            timeout_seconds=_int(timeout_seconds, 900),
        )
    except ForgeError as exc:
        return _redirect(f"/projects/{slug}", err=exc.message)

    note = (
        "Scheduled jobs run against whatever is serving production."
        if kind is ProcessType.CRON
        else "Workers start on the next deploy or promotion."
    )
    return _redirect(f"/projects/{slug}", ok=f"Added {name}. {note}")


@router.get("/projects/{slug}/processes/{name}", response_class=HTMLResponse)
async def process_page(
    request: Request, slug: str, name: str, ok: str = "", err: str = ""
):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    process = await process_repo.get_by_name(project.id, name)
    return _render(
        request,
        "process.html",
        {
            "project": project,
            "process": await _decorate(process),
            "runs": await process_repo.list_runs(process.id, limit=25),
            "ok": ok,
            "err": err,
        },
    )


@router.post("/projects/{slug}/processes/{name}/toggle")
async def toggle_process(request: Request, slug: str, name: str):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    process = await process_repo.get_by_name(project.id, name)
    await process_repo.update(process.id, {"enabled": not process.enabled})
    state = "paused" if process.enabled else "resumed"
    return _redirect(f"/projects/{slug}", ok=f"{name} {state}.")


@router.post("/projects/{slug}/processes/{name}/run")
async def run_process_now(request: Request, slug: str, name: str):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    process = await process_repo.get_by_name(project.id, name)
    if process.type is not ProcessType.CRON:
        return _redirect(
            f"/projects/{slug}/processes/{name}",
            err=f"{name} runs continuously, so there is nothing to trigger.",
        )

    # The manual run takes the current minute's slot, so pressing this at
    # 02:59:58 on a job due at 03:00 produces one run rather than two.
    slot = datetime.now(UTC).replace(second=0, microsecond=0)
    run = await process_repo.claim_slot(
        process.id, slot, project.production_deployment_id
    )
    if run is None:
        return _redirect(
            f"/projects/{slug}/processes/{name}",
            err="Already queued or running for this minute.",
        )
    return _redirect(
        f"/projects/{slug}/processes/{name}", ok="Queued — it starts within seconds."
    )


@router.post("/projects/{slug}/processes/{name}/delete")
async def delete_process(request: Request, slug: str, name: str):
    signed_in(request)
    project = await project_repo.get_by_slug(slug)
    process = await process_repo.get_by_name(project.id, name)
    await process_repo.delete(process.id)
    return _redirect(f"/projects/{slug}", ok=f"Removed {name}.")


async def _decorate(process):
    """Attach the two derived things the templates want.

    A dict rather than a richer model: these are presentation, and putting
    "next run in four hours" on the domain object would make it depend on the
    current time.
    """
    description = None
    if process.runs_on_a_schedule:
        try:
            description = describe(parse(process.schedule))
        except InvalidSchedule:
            description = "unreadable schedule"
    return {
        "process": process,
        "description": description,
        "next_run_at": process_engine.next_due(process) if process.enabled else None,
        "last_run": await process_repo.last_run(process.id),
    }


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------


@router.get("/deployments/{short_id}", response_class=HTMLResponse)
async def deployment_page(
    request: Request,
    short_id: str,
    settings: SettingsDep,
    ok: str = "",
    err: str = "",
):
    signed_in(request)
    deployment = await deployment_repo.get_by_short_id(short_id)
    project = await project_repo.get(deployment.project_id)
    logs = await deployment_repo.read_logs(deployment.id, limit=4000)

    production = None
    if project.production_deployment_id:
        production = await deployment_repo.get(project.production_deployment_id)

    return _render(
        request,
        "deployment.html",
        {
            "project": project,
            "deployment": deployment,
            "logs": logs,
            "cursor": logs[-1].seq if logs else 0,
            "url": settings.deployment_url(deployment.short_id),
            "is_production": project.production_deployment_id == deployment.id,
            "is_older": bool(production and deployment.number < production.number),
            "duration": _duration(deployment),
            "ok": ok,
            "err": err,
        },
    )


@router.post("/deployments/{short_id}/promote")
async def promote(request: Request, short_id: str, settings: SettingsDep):
    signed_in(request)
    deployment = await deployment_repo.get_by_short_id(short_id)
    project = await project_repo.get(deployment.project_id)
    log = await LogWriter.resume(deployment.id)
    try:
        await promote_engine.promote(
            project,
            deployment,
            settings=settings,
            log=log,
            workdir=settings.build_root / deployment.short_id,
        )
    except ForgeError as exc:
        return _redirect(f"/deployments/{short_id}", err=exc.message)
    finally:
        await log.flush()
    return _redirect(
        f"/projects/{project.slug}",
        ok=f"Production now serves #{deployment.number}.",
    )


@router.post("/deployments/{short_id}/redeploy")
async def redeploy(request: Request, short_id: str):
    signed_in(request)
    deployment = await deployment_repo.get_by_short_id(short_id)
    project = await project_repo.get(deployment.project_id)
    queued = await service.redeploy(project, deployment)
    return _redirect(f"/deployments/{queued.short_id}")


@router.post("/deployments/{short_id}/cancel")
async def cancel(request: Request, short_id: str):
    signed_in(request)
    deployment = await deployment_repo.get_by_short_id(short_id)
    if deployment.status.is_terminal or deployment.status.is_in_flight:
        return _redirect(
            f"/deployments/{short_id}",
            err=f"This deployment is {deployment.status.value} and cannot be cancelled.",
        )
    await deployment_repo.mark_cancelled(deployment.id)
    return _redirect(f"/deployments/{short_id}", ok="Cancelled.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _summarise(project: Project, settings: Settings) -> ProjectSummary:
    live = None
    if project.production_deployment_id:
        live = await deployment_repo.get(project.production_deployment_id)

    # A verified domain is only the project's address once something is
    # serving it. Showing it before the first deploy points at a hostname
    # that answers with the router's default, which reads as "the site is
    # broken" rather than "the site does not exist yet".
    primary = None
    if live is not None:
        for domain in await project_repo.list_domains(project.id):
            if domain.is_verified and (domain.is_primary or primary is None):
                primary = domain.host
                if domain.is_primary:
                    break

    return ProjectSummary(
        project=project,
        live=live,
        live_url=settings.deployment_url(live.short_id) if live else "",
        primary_domain=primary,
    )


def _render(request: Request, template: str, context: dict, *, nav: bool = True):
    return templates.TemplateResponse(request, template, {"show_nav": nav, **context})


def _redirect(path: str, *, ok: str = "", err: str = "") -> RedirectResponse:
    """Post-redirect-get, with the message carried in the query string.

    No flash storage: there is no server-side session to put one in, and
    adding one so a sentence can survive a redirect would be a table, a
    cleanup job and a shared-state problem in exchange for a tidier URL.
    """
    if ok:
        path += ("&" if "?" in path else "?") + "ok=" + quote(ok)
    elif err:
        path += ("&" if "?" in path else "?") + "err=" + quote(err)
    return RedirectResponse(path, status_code=303)


def _int(raw: str, fallback: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback


def _duration(deployment: Deployment) -> str | None:
    if not deployment.started_at or not deployment.finished_at:
        return None
    seconds = (deployment.finished_at - deployment.started_at).total_seconds()
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


# Re-exported for the type checker's benefit; the templates use them by name.
__all__ = ["Deployment", "Domain", "EnvVar", "NeedsLogin", "Project", "router"]
