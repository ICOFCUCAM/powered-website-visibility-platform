"""The Docker daemon, driven through its CLI.

The CLI rather than the SDK, for one reason that outweighs the tidiness of a
library: BuildKit. Cache mounts, secret mounts and `--progress=plain` are how
builds stay fast and how build-time secrets stay out of image layers, and the
Python SDK's build support predates all of it.

Everything here is a thin, single-purpose coroutine over a subprocess, which
also makes the whole module replaceable by a fake in tests — there is no
hidden state and no connection to hold.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

LogSink = Callable[[str], Awaitable[None]]

#: Label carried by everything Forge creates, so reconciliation can find its
#: own containers without mistaking a hand-started one for an orphan.
OWNER_LABEL = "forge.owner"
OWNER_VALUE = "forge"
DEPLOYMENT_LABEL = "forge.deployment"
PROJECT_LABEL = "forge.project"


class DockerError(RuntimeError):
    """A docker command exited non-zero. The output is in the message."""


@dataclass(frozen=True, slots=True)
class RunSpec:
    image: str
    name: str
    network: str
    #: The deployment's permanent hostname. Never changes, so it can live in a
    #: container label; production domains cannot, and go through the router's
    #: file provider instead.
    host: str
    port: int
    router: str
    memory_mb: int
    cpu_shares: float
    cert_resolver: str
    env_file: Path | None = None
    #: Values that cannot be expressed in an env file — anything containing a
    #: newline. Passed as arguments instead. See forge.engine.environment.
    inline_env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


async def build(
    *,
    context: Path,
    dockerfile: Path,
    tag: str,
    secret_env_file: Path | None,
    log: LogSink,
    timeout: int,
) -> None:
    """Build an image, streaming every line to `log` as it happens.

    The environment file is passed as a BuildKit secret, not a build argument.
    A build argument is recorded in the image's metadata and `docker history`
    prints it; a secret mount exists only for the RUN instruction that asks
    for it and is in no layer afterwards.
    """
    args = [
        "build",
        "--progress=plain",
        "--file",
        str(dockerfile),
        "--tag",
        tag,
        # Without this, a rebuild after a base image update silently keeps
        # serving the old base. Deploys are infrequent enough that the cost of
        # checking is irrelevant next to shipping a stale CVE.
        "--pull",
    ]
    if secret_env_file is not None:
        args += ["--secret", f"id=env,src={secret_env_file}"]
    args.append(str(context))

    await _stream(args, log=log, timeout=timeout, buildkit=True)


async def run(spec: RunSpec, *, log: LogSink | None = None) -> str:
    """Start a container and return its id.

    The container publishes no port. It is reachable only from the shared
    network, by the router, by container name — so a deployment cannot be
    reached except through the router, and two deployments cannot collide on a
    host port because neither has one.
    """
    labels = {
        OWNER_LABEL: OWNER_VALUE,
        "traefik.enable": "true",
        "traefik.docker.network": spec.network,
        f"traefik.http.routers.{spec.router}.rule": f"Host(`{spec.host}`)",
        f"traefik.http.services.{spec.router}.loadbalancer.server.port": str(spec.port),
        **spec.labels,
    }
    if spec.cert_resolver:
        labels[f"traefik.http.routers.{spec.router}.entrypoints"] = "websecure"
        # `tls=true`, and deliberately no certResolver: the router inherits the
        # entrypoint's default certificate, which is the wildcard covering the
        # whole deploy domain.
        #
        # Naming a resolver here would instead ask the CA for a certificate per
        # deployment hostname. Let's Encrypt issues 50 new certificates per
        # registered domain per week, and this platform mints a brand new
        # hostname on every deploy — so about seven deploys a day would exhaust
        # the week and then fail to issue anything at all, including for the
        # custom domains that carry the real traffic. One wildcard, issued once,
        # has no such ceiling.
        labels[f"traefik.http.routers.{spec.router}.tls"] = "true"
    else:
        labels[f"traefik.http.routers.{spec.router}.entrypoints"] = "web"

    args = [
        "run",
        "--detach",
        "--name",
        spec.name,
        "--network",
        spec.network,
        # Survives a host reboot without the control plane having to notice.
        "--restart",
        "unless-stopped",
        f"--memory={spec.memory_mb}m",
        f"--cpus={spec.cpu_shares}",
        # A fork bomb in one site should cost that site and nothing else.
        "--pids-limit=512",
        # Nothing built here needs a capability, and a process that cannot
        # acquire new privileges cannot escalate through a setuid binary it
        # happens to find in its own image.
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        # Container logs are the run stream. Capped so a chatty app cannot
        # fill the host disk and take every other site down with it.
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=3",
        f"--env=PORT={spec.port}",
    ]
    for key, value in labels.items():
        args += ["--label", f"{key}={value}"]
    if spec.env_file is not None:
        args += ["--env-file", str(spec.env_file)]
    for key, value in spec.inline_env.items():
        args += ["--env", f"{key}={value}"]
    args.append(spec.image)

    out = await _capture(args)
    container_id = out.strip().splitlines()[-1]
    if log:
        await log(f"started container {container_id[:12]}")
    return container_id


async def stop(container_id: str, *, timeout: int = 10) -> None:
    """Stop a container, tolerating one that is already gone."""
    try:
        await _capture(["stop", "--time", str(timeout), container_id])
    except DockerError as exc:
        if "No such container" not in str(exc):
            raise


async def remove(container_id: str, *, force: bool = True) -> None:
    args = ["rm"] + (["--force"] if force else []) + [container_id]
    try:
        await _capture(args)
    except DockerError as exc:
        if "No such container" not in str(exc):
            raise


async def remove_by_name(name: str) -> None:
    """Clear a name before reusing it.

    A previous deploy that died between `docker run` and the database write
    leaves a container holding the name; without this the retry fails on a
    name conflict and looks like a build problem.
    """
    await remove(name, force=True)


async def is_running(container_id: str) -> bool:
    try:
        out = await _capture(["inspect", "--format", "{{.State.Running}}", container_id])
    except DockerError:
        return False
    return out.strip() == "true"


async def container_ip(container_id: str, network: str) -> str | None:
    """The container's address on the shared network.

    Used by the health check, which talks to the container directly rather
    than through the router — the question being asked is "is this app up",
    and going through the router would also be asking "is the router's
    configuration right", which is a different question with a different fix.
    """
    template = (
        "{{with index .NetworkSettings.Networks " + json.dumps(network) + "}}"
        "{{.IPAddress}}{{end}}"
    )
    try:
        out = await _capture(["inspect", "--format", template, container_id])
    except DockerError:
        return None
    return out.strip() or None


async def logs(container_id: str, *, tail: int = 200) -> str:
    try:
        return await _capture(["logs", "--tail", str(tail), container_id])
    except DockerError as exc:
        return f"(could not read container logs: {exc})"


async def ensure_network(name: str) -> None:
    try:
        await _capture(["network", "inspect", name])
    except DockerError:
        await _capture(["network", "create", name])


async def list_owned() -> list[dict[str, str]]:
    """Every container Forge started, for reconciliation at boot.

    The database is the intent; Docker is the fact. They diverge whenever the
    host reboots, a container is killed by the OOM reaper, or someone runs
    `docker rm` by hand — so the worker compares them on startup rather than
    trusting the rows.
    """
    out = await _capture(
        [
            "ps",
            "--all",
            "--filter",
            f"label={OWNER_LABEL}={OWNER_VALUE}",
            "--format",
            "{{json .}}",
        ]
    )
    found = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            found.append(json.loads(line))
    return found


async def image_exists(tag: str) -> bool:
    try:
        await _capture(["image", "inspect", tag])
    except DockerError:
        return False
    return True


async def prune_image(tag: str) -> None:
    # An image still referenced by a container is not an error worth raising:
    # it means something is still using it, which is correct.
    with contextlib.suppress(DockerError):
        await _capture(["image", "rm", tag])


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------


def _env(buildkit: bool) -> dict[str, str]:
    env = dict(os.environ)
    if buildkit:
        env["DOCKER_BUILDKIT"] = "1"
    return env


async def _stream(args: list[str], *, log: LogSink, timeout: int, buildkit: bool) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_env(buildkit),
    )
    assert proc.stdout is not None

    async def pump() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line:
                await log(line)

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        await log(f"timed out after {timeout}s — killed")
        raise DockerError(f"docker {args[0]} timed out after {timeout}s") from exc

    if proc.returncode != 0:
        raise DockerError(f"docker {args[0]} failed with exit code {proc.returncode}")


async def _capture(args: list[str], *, timeout: int = 120) -> str:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env(buildkit=False),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        raise DockerError(f"docker {args[0]} timed out") from exc
    if proc.returncode != 0:
        raise DockerError(
            f"docker {shlex.join(args[:2])} failed ({proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()}"
        )
    return stdout.decode(errors="replace")


# ---------------------------------------------------------------------------
# Processes: workers and one-shot jobs
# ---------------------------------------------------------------------------

PROCESS_LABEL = "forge.process"
ROLE_LABEL = "forge.role"

#: How much of a job's output to keep. Enough to diagnose a failure, bounded
#: so a job that prints a megabyte a second cannot fill the database. The tail
#: is kept rather than the head: the traceback is at the end.
MAX_OUTPUT_BYTES = 64_000


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """A container that is not a website.

    Shares the deployment's image, and deliberately carries no Traefik labels
    at all — a queue consumer with a public hostname is a mistake waiting to
    be found by a crawler.
    """

    image: str
    name: str
    network: str
    command: str | None
    memory_mb: int
    env_file: Path | None = None
    inline_env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaskResult:
    exit_code: int | None
    output: str
    timed_out: bool

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def command_argv(command: str | None) -> list[str]:
    """Turn a configured command into arguments for `docker run`.

    A command needing shell features gets a shell, explicitly. Everything else
    is split and passed directly, which matters more than it looks: a
    distroless image has no `/bin/sh`, so wrapping every command in one would
    make cron impossible on exactly the images this platform builds for Go.
    """
    if not command:
        return []
    if any(ch in command for ch in "|&;<>$*?`"):
        return ["/bin/sh", "-c", f"exec {command}"]
    return shlex.split(command)


def _process_args(spec: TaskSpec) -> list[str]:
    args = [
        "--name",
        spec.name,
        "--network",
        spec.network,
        f"--memory={spec.memory_mb}m",
        "--pids-limit=512",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
    ]
    for key, value in spec.labels.items():
        args += ["--label", f"{key}={value}"]
    if spec.env_file is not None:
        args += ["--env-file", str(spec.env_file)]
    for key, value in spec.inline_env.items():
        args += ["--env", f"{key}={value}"]
    return args


async def run_worker(spec: TaskSpec) -> str:
    """Start a long-running process that serves no traffic."""
    await remove_by_name(spec.name)
    args = [
        "run",
        "--detach",
        "--restart",
        "unless-stopped",
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=3",
        # Explicit rather than merely absent: the Docker provider ignores
        # containers without this, but saying so leaves no doubt for anyone
        # reading `docker inspect` and wondering why the worker has no route.
        "--label",
        "traefik.enable=false",
        *_process_args(spec),
        spec.image,
        *command_argv(spec.command),
    ]
    out = await _capture(args)
    return out.strip().splitlines()[-1]


async def run_once(spec: TaskSpec, *, timeout: int) -> TaskResult:
    """Run a container to completion and return its exit code and output.

    Not `--rm`: the container is removed explicitly at the end, because a
    `--rm` container that is killed on timeout can disappear before its exit
    status is read, and "the job timed out" and "the job vanished" would
    become the same report.
    """
    await remove_by_name(spec.name)
    args = [
        "run",
        *_process_args(spec),
        "--label",
        "traefik.enable=false",
        spec.image,
        *command_argv(spec.command),
    ]

    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_env(buildkit=False),
    )
    assert proc.stdout is not None

    collected = bytearray()
    timed_out = False

    async def drain() -> None:
        assert proc.stdout is not None
        async for chunk in proc.stdout:
            collected.extend(chunk)
            # Trim as we go. Buffering the whole of a runaway job's output and
            # truncating at the end would mean holding it all in memory first,
            # which is the thing being defended against.
            if len(collected) > MAX_OUTPUT_BYTES * 2:
                del collected[: len(collected) - MAX_OUTPUT_BYTES]

    try:
        await asyncio.wait_for(asyncio.gather(drain(), proc.wait()), timeout=timeout)
    except TimeoutError:
        timed_out = True
        with contextlib.suppress(DockerError):
            await _capture(["kill", spec.name])
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=15)

    exit_code = None
    if not timed_out:
        with contextlib.suppress(DockerError):
            raw = await _capture(
                ["inspect", "--format", "{{.State.ExitCode}}", spec.name]
            )
            exit_code = int(raw.strip())

    await remove(spec.name, force=True)

    output = collected.decode(errors="replace")
    if len(collected) >= MAX_OUTPUT_BYTES:
        output = "… earlier output trimmed …\n" + output[-MAX_OUTPUT_BYTES:]

    return TaskResult(exit_code=exit_code, output=output, timed_out=timed_out)


async def list_process_containers(project_slug: str) -> list[str]:
    """Names of the worker containers currently running for a project."""
    out = await _capture(
        [
            "ps",
            "--all",
            "--format",
            "{{.Names}}",
            "--filter",
            f"label={PROJECT_LABEL}={project_slug}",
            "--filter",
            f"label={ROLE_LABEL}=worker",
        ]
    )
    return [line.strip() for line in out.splitlines() if line.strip()]
