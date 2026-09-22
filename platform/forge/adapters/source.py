"""Getting the code onto the disk.

Shallow, single-branch clones. A deploy needs one commit's tree and nothing
else, and cloning years of history to build one of them is the difference
between a deploy that starts in two seconds and one that starts in ninety.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from forge.domain.errors import InvalidRequest
from forge.domain.repo_url import validate_repo_url

LogSink = Callable[[str], Awaitable[None]]

#: Re-exported so callers that already hold an adapter need not reach past it.
__all__ = ["Commit", "fetch", "resolve_head", "validate_repo_url"]

#: Long enough to clone a large repository over a slow link, short enough that
#: a hung transport does not hold a worker until someone notices.
CLONE_TIMEOUT = 300


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    ref: str
    message: str
    author: str


async def fetch(
    repo_url: str,
    ref: str,
    dest: Path,
    *,
    sha: str | None = None,
    log: LogSink | None = None,
) -> Commit:
    """Clone `repo_url` at `ref` into `dest` and report what was fetched.

    When `sha` is given the clone is pinned to it. This matters for rollback
    and for a redeploy: both must rebuild the commit that was asked for, not
    whatever the branch has moved on to since.
    """
    validate_repo_url(repo_url)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    await _git(
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--branch",
        ref,
        "--no-tags",
        repo_url,
        str(dest),
        log=log,
    )

    if sha:
        # A shallow clone of a branch does not contain an arbitrary commit, so
        # it is fetched on its own. Servers that refuse this (uploadpack
        # .allowReachableSHA1InWant off) make the deepen the fallback.
        try:
            await _git("fetch", "--depth", "1", "origin", sha, cwd=dest, log=log)
        except RuntimeError:
            await _git("fetch", "--unshallow", "origin", cwd=dest, log=log)
        await _git("checkout", "--detach", sha, cwd=dest, log=log)

    resolved = await _capture("rev-parse", "HEAD", cwd=dest)
    message = await _capture("log", "-1", "--pretty=%s", cwd=dest)
    author = await _capture("log", "-1", "--pretty=%an", cwd=dest)

    # The platform builds a tree, not a repository. Leaving .git behind would
    # put it in the Docker build context, which both slows the build and bakes
    # the full history into any image whose Dockerfile says `COPY . .`.
    shutil.rmtree(dest / ".git", ignore_errors=True)

    return Commit(sha=resolved, ref=ref, message=message, author=author)


async def resolve_head(repo_url: str, ref: str) -> str:
    """The sha at the tip of `ref`, without cloning anything.

    Used when a deploy is requested by branch: the deployment row records the
    exact commit from the start, so the history never says "deployed main" and
    leaves which commit to the imagination.
    """
    validate_repo_url(repo_url)
    out = await _capture("ls-remote", "--heads", repo_url, ref)
    if not out:
        raise InvalidRequest(f"Branch {ref!r} does not exist in {repo_url}")
    return out.split()[0]


async def _git(*args: str, cwd: Path | None = None, log: LogSink | None = None) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_env(),
    )
    assert proc.stdout is not None
    lines: list[str] = []
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        lines.append(line)
        if log and line:
            await log(line)
    try:
        await asyncio.wait_for(proc.wait(), timeout=CLONE_TIMEOUT)
    except TimeoutError as exc:
        proc.kill()
        raise RuntimeError(f"git {args[0]} timed out after {CLONE_TIMEOUT}s") from exc
    if proc.returncode != 0:
        tail = "\n".join(lines[-10:])
        raise RuntimeError(f"git {args[0]} failed ({proc.returncode}):\n{tail}")


async def _capture(*args: str, cwd: Path | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env(),
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLONE_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {args[0]} failed ({proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()}"
        )
    return stdout.decode(errors="replace").strip()


def _env() -> dict[str, str]:
    """Git, told never to ask a human anything.

    A clone that prompts for a password in a worker process hangs until the
    timeout instead of failing, and the deployment log shows nothing at all.
    """

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    env.setdefault(
        "GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    )
    return env
