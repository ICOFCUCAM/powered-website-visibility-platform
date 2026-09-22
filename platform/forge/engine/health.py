"""Deciding whether a freshly started container is actually serving.

A container that is "running" has told you only that its PID 1 has not exited.
An app that crashed on a bad DATABASE_URL and is being restarted every two
seconds by Docker's restart policy is running by that definition, most of the
time. The question worth asking is whether it answers HTTP.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from forge.adapters import containers

#: Start quickly, back off, never wait more than a couple of seconds between
#: tries. A static site answers on the first probe; a JVM might take thirty
#: seconds, and polling it every 100ms for all of them is pure noise.
INITIAL_INTERVAL = 0.2
MAX_INTERVAL = 2.0
BACKOFF = 1.5


@dataclass(frozen=True, slots=True)
class HealthResult:
    healthy: bool
    detail: str
    attempts: int
    elapsed_seconds: float


async def wait_until_healthy(
    *,
    container_id: str,
    network: str,
    port: int,
    path: str,
    timeout: int,
) -> HealthResult:
    """Poll the container directly until it answers, or time out.

    Any HTTP response counts, including 404 and 500. The check is "is there a
    server on this port", not "is the site correct" — a site whose home page
    legitimately 404s (an API with no root route) would otherwise never deploy,
    and diagnosing a 500 is the owner's job, not the platform's.

    A container that has *exited* fails immediately rather than waiting out the
    timeout, because the answer is already known and its logs already say why.
    """
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    interval = INITIAL_INTERVAL
    attempts = 0
    last = "no attempt completed"

    address = await containers.container_ip(container_id, network)
    if address is None:
        return HealthResult(
            healthy=False,
            detail=(
                f"The container was not attached to the {network!r} network, "
                "so the router could never reach it."
            ),
            attempts=0,
            elapsed_seconds=0.0,
        )

    url = f"http://{address}:{port}{path}"
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        while time.monotonic() < deadline:
            attempts += 1
            try:
                response = await client.get(url)
                return HealthResult(
                    healthy=True,
                    detail=f"answered {response.status_code} on {path}",
                    attempts=attempts,
                    elapsed_seconds=time.monotonic() - started,
                )
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"

            if not await containers.is_running(container_id):
                return HealthResult(
                    healthy=False,
                    detail="the container exited before it served a request",
                    attempts=attempts,
                    elapsed_seconds=time.monotonic() - started,
                )

            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(interval * BACKOFF, MAX_INTERVAL)

    return HealthResult(
        healthy=False,
        detail=(
            f"no HTTP response on port {port} within {timeout}s (last error: {last})"
        ),
        attempts=attempts,
        elapsed_seconds=time.monotonic() - started,
    )
