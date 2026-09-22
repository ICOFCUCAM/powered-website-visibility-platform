"""The command line, for the operator sitting on the host.

Talks to the database and the engine directly rather than to the HTTP API. The
reason is the situation it is most needed in: the API will not start, or
something is wrong with the very deployment that serves the dashboard. A tool
that depends on the thing being repaired is no use during the repair.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from forge.adapters import containers, crypto, db
from forge.config import ConfigError, Settings, get_settings
from forge.domain import naming
from forge.domain.errors import ForgeError
from forge.domain.models import DeploymentTrigger, EnvTarget
from forge.engine import promote as promote_engine
from forge.engine import service, verify
from forge.engine.logs import LogWriter
from forge.repositories import deployments as deployment_repo
from forge.repositories import projects as project_repo

MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if not getattr(args, "handler", None):
        parser.print_help()
        raise SystemExit(2)
    try:
        asyncio.run(_dispatch(args))
    except (ForgeError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None


async def _dispatch(args: argparse.Namespace) -> None:
    # `keygen` deliberately runs before settings are read: it exists to
    # produce the key that reading settings would demand.
    if args.handler is cmd_keygen:
        await cmd_keygen(args, None)
        return

    settings = get_settings()
    await db.open_pool(settings.database_url, min_size=1, max_size=4)
    try:
        await args.handler(args, settings)
    finally:
        await db.close_pool()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def cmd_keygen(args: argparse.Namespace, settings: Settings | None) -> None:
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())


async def cmd_migrate(args: argparse.Namespace, settings: Settings) -> None:
    """Apply every migration not yet recorded, in filename order.

    Each one runs inside the same transaction that records it, so a migration
    that fails halfway leaves neither the change nor the record of it — the
    thing that makes re-running safe.
    """
    async with db.connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur = await conn.execute("SELECT filename FROM schema_migrations")
        applied = {row["filename"] for row in await cur.fetchall()}

    pending = [p for p in sorted(MIGRATIONS.glob("*.sql")) if p.name not in applied]
    if not pending:
        print("database is up to date")
        return

    for path in pending:
        print(f"applying {path.name} … ", end="", flush=True)
        async with db.pool().connection() as conn:
            await conn.set_autocommit(False)
            try:
                await conn.execute(path.read_text())
                await conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (path.name,),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                print("failed")
                raise
        print("ok")


async def cmd_project_create(args: argparse.Namespace, settings: Settings) -> None:
    project = await project_repo.create(
        slug=args.slug or naming.slugify(args.name),
        name=args.name,
        repo_url=args.repo,
        production_branch=args.branch,
        root_directory=args.root or "",
    )
    print(f"created {project.slug} ({project.id})")
    print(f"  webhook  POST /webhooks/{project.slug}")
    print(f"  secret   {project.webhook_secret}")


async def cmd_project_list(args: argparse.Namespace, settings: Settings) -> None:
    projects = await project_repo.list_all()
    if not projects:
        print("no projects yet — forge project create --name … --repo …")
        return
    for project in projects:
        live = "—"
        if project.production_deployment_id:
            deployment = await deployment_repo.get(project.production_deployment_id)
            live = f"#{deployment.number} {deployment.git_sha[:8]}"
        print(f"{project.slug:24} {project.production_branch:12} production {live}")


async def cmd_deploy(args: argparse.Namespace, settings: Settings) -> None:
    project = await project_repo.resolve(args.project)
    deployment = await service.queue_deploy(
        project, ref=args.ref, trigger=DeploymentTrigger.MANUAL
    )
    print(f"queued #{deployment.number} — {deployment.short_id}")
    print(f"  {deployment.git_ref} at {deployment.git_sha[:8]}")
    print(f"  url {settings.deployment_url(deployment.short_id)}")
    print(f"  logs: forge logs {deployment.short_id} --follow")


async def cmd_deployments(args: argparse.Namespace, settings: Settings) -> None:
    project = await project_repo.resolve(args.project)
    for deployment in await deployment_repo.list_for_project(
        project.id, limit=args.limit
    ):
        marker = "*" if project.production_deployment_id == deployment.id else " "
        live = "live" if deployment.is_live else "    "
        print(
            f"{marker} #{deployment.number:<4} {deployment.status.value:10} {live} "
            f"{deployment.git_sha[:8]} {deployment.git_ref:16} {deployment.short_id}"
        )
        if deployment.error:
            print(f"      {deployment.error}")


async def cmd_logs(args: argparse.Namespace, settings: Settings) -> None:
    deployment = await deployment_repo.get_by_short_id(args.deployment)
    cursor = 0
    while True:
        lines = await deployment_repo.read_logs(deployment.id, after=cursor)
        for line in lines:
            cursor = line.seq
            prefix = "··" if line.stream.value == "system" else "  "
            print(f"{prefix} {line.line}")
        if not args.follow:
            return
        current = await deployment_repo.get(deployment.id)
        if current.status.is_terminal and not lines:
            print(f"-- {current.status.value}")
            return
        await asyncio.sleep(0.5)


async def cmd_promote(args: argparse.Namespace, settings: Settings) -> None:
    deployment = await _resolve_deployment(args.project, args.deployment)
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
    finally:
        await log.flush()
    print(f"production now serves #{deployment.number} ({deployment.git_sha[:8]})")


async def cmd_env_set(args: argparse.Namespace, settings: Settings) -> None:
    project = await project_repo.resolve(args.project)
    value = args.value
    if value == "-":
        # Reading from stdin keeps the secret out of the shell history and out
        # of the process list, which `--value` cannot.
        value = sys.stdin.read().rstrip("\n")
    await project_repo.set_env(
        project.id,
        args.key,
        crypto.encrypt(value, key=settings.master_key),
        EnvTarget(args.target),
    )
    print(f"set {args.key} ({args.target}) — takes effect on the next build")


async def cmd_env_list(args: argparse.Namespace, settings: Settings) -> None:
    project = await project_repo.resolve(args.project)
    for var in await project_repo.list_env(project.id):
        print(f"{var.key:32} {var.target.value}")


async def cmd_env_rm(args: argparse.Namespace, settings: Settings) -> None:
    project = await project_repo.resolve(args.project)
    removed = await project_repo.delete_env(project.id, args.key, None)
    print(f"removed {removed} entr{'y' if removed == 1 else 'ies'}")


async def cmd_domain_add(args: argparse.Namespace, settings: Settings) -> None:
    project = await project_repo.resolve(args.project)
    domain = await project_repo.add_domain(project.id, args.host, primary=args.primary)
    print(f"added {domain.host}")
    print(f"  point it here:  CNAME {domain.host} -> {settings.deploy_domain}")
    print(f"  then:           forge domain verify {project.slug} {domain.host}")


async def cmd_domain_verify(args: argparse.Namespace, settings: Settings) -> None:
    project = await project_repo.resolve(args.project)
    domains = {d.host: d for d in await project_repo.list_domains(project.id)}
    domain = domains.get(args.host.lower())
    if domain is None:
        print(f"{args.host} is not attached to {project.slug}")
        raise SystemExit(1)

    result = await verify.verify(domain.host, expected_host=settings.deploy_domain)
    print(result.detail)
    if not result.verified:
        raise SystemExit(1)

    await project_repo.mark_domain_verified(domain.id)
    from forge.engine import routing

    print(await routing.refresh(await project_repo.get(project.id), settings=settings))


async def cmd_webhook(args: argparse.Namespace, settings: Settings) -> None:
    project = await project_repo.resolve(args.project)
    print(f"Payload URL   {settings.scheme}://<your forge host>/webhooks/{project.slug}")
    print("Content type  application/json")
    print(f"Secret        {project.webhook_secret}")


async def cmd_doctor(args: argparse.Namespace, settings: Settings) -> None:
    """Check the things that are wrong when nothing deploys."""
    print(f"database            ok ({settings.environment})")
    print(f"deploy domain       *.{settings.deploy_domain} over {settings.scheme}")
    if not settings.tls_enabled:
        print("  warning           TLS is off — every site is served over plain HTTP")

    try:
        await containers.ensure_network(settings.network)
        print(f"docker              ok (network {settings.network!r} present)")
    except Exception as exc:  # noqa: BLE001 - reporting, not handling
        print(f"docker              UNREACHABLE — {exc}")

    for label, path in (
        ("build root", settings.build_root),
        ("router config", settings.router_config_dir),
    ):
        writable = "ok" if _writable(path) else "NOT WRITABLE"
        print(f"{label:20}{writable} ({path})")

    result = await verify._resolve(settings.deploy_domain)
    if result:
        print(f"wildcard DNS        resolves to {', '.join(result)}")
    else:
        print(f"wildcard DNS        {settings.deploy_domain} does not resolve")

    stuck = await deployment_repo.reclaim_abandoned(
        older_than_seconds=settings.build_timeout_seconds + 120
    )
    if stuck:
        print(f"abandoned builds    failed {len(stuck)} stuck deployment(s)")


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".forge-write-test"
        probe.touch()
        probe.unlink()
    except OSError:
        return False
    return True


async def _resolve_deployment(project_ref: str, reference: str):
    """Accept `#12`, `12` or a short id, because all three get typed."""
    project = await project_repo.resolve(project_ref)
    stripped = reference.lstrip("#")
    if stripped.isdigit():
        wanted = int(stripped)
        for deployment in await deployment_repo.list_for_project(project.id, limit=500):
            if deployment.number == wanted:
                return deployment
        raise ForgeError(f"{project.slug} has no deployment #{wanted}")
    return await deployment_repo.get_by_short_id(reference)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge", description=__doc__)
    sub = parser.add_subparsers()

    sub.add_parser("keygen", help="print a new master key").set_defaults(
        handler=cmd_keygen
    )
    sub.add_parser("migrate", help="apply database migrations").set_defaults(
        handler=cmd_migrate
    )
    sub.add_parser("doctor", help="check the host's configuration").set_defaults(
        handler=cmd_doctor
    )

    project = sub.add_parser("project", help="manage projects").add_subparsers()
    create = project.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--repo", required=True)
    create.add_argument("--slug")
    create.add_argument("--branch", default="main")
    create.add_argument("--root", help="subdirectory the app lives in")
    create.set_defaults(handler=cmd_project_create)
    project.add_parser("list").set_defaults(handler=cmd_project_list)

    deploy = sub.add_parser("deploy", help="queue a deployment")
    deploy.add_argument("project")
    deploy.add_argument("--ref", help="branch to deploy (default: production branch)")
    deploy.set_defaults(handler=cmd_deploy)

    listing = sub.add_parser("deployments", help="list a project's deployments")
    listing.add_argument("project")
    listing.add_argument("--limit", type=int, default=20)
    listing.set_defaults(handler=cmd_deployments)

    logs = sub.add_parser("logs", help="read a deployment's log")
    logs.add_argument("deployment")
    logs.add_argument("--follow", "-f", action="store_true")
    logs.set_defaults(handler=cmd_logs)

    promote = sub.add_parser("promote", help="point production at a deployment")
    promote.add_argument("project")
    promote.add_argument("deployment", help="#number or short id")
    promote.set_defaults(handler=cmd_promote)

    env = sub.add_parser("env", help="manage environment variables").add_subparsers()
    env_set = env.add_parser("set")
    env_set.add_argument("project")
    env_set.add_argument("key")
    env_set.add_argument("value", help="the value, or - to read from stdin")
    env_set.add_argument("--target", choices=[t.value for t in EnvTarget], default="all")
    env_set.set_defaults(handler=cmd_env_set)
    env_list = env.add_parser("list")
    env_list.add_argument("project")
    env_list.set_defaults(handler=cmd_env_list)
    env_rm = env.add_parser("rm")
    env_rm.add_argument("project")
    env_rm.add_argument("key")
    env_rm.set_defaults(handler=cmd_env_rm)

    domain = sub.add_parser("domain", help="manage custom domains").add_subparsers()
    domain_add = domain.add_parser("add")
    domain_add.add_argument("project")
    domain_add.add_argument("host")
    domain_add.add_argument("--primary", action="store_true")
    domain_add.set_defaults(handler=cmd_domain_add)
    domain_verify = domain.add_parser("verify")
    domain_verify.add_argument("project")
    domain_verify.add_argument("host")
    domain_verify.set_defaults(handler=cmd_domain_verify)

    webhook = sub.add_parser("webhook", help="print a project's webhook settings")
    webhook.add_argument("project")
    webhook.set_defaults(handler=cmd_webhook)

    return parser


if __name__ == "__main__":
    main()
