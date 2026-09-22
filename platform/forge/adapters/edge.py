"""Production routing, as files the edge router watches.

A deployment's own permanent hostname is a container label, set once at
`docker run` and never touched again. Production domains cannot work that way,
because Docker has no way to change a running container's labels — promoting a
deployment would mean destroying and recreating it, which is a restart, a cold
cache and a few seconds of 502 for every promotion and every rollback.

So production domains live in Traefik's file provider instead. Promotion
becomes: write one small JSON file naming which container the site's domains
point at, atomically. Traefik notices within its watch interval and moves the
traffic with no container touched at all. Rollback is the same write with an
older service name, which is why it takes about as long as saving a file.

JSON rather than YAML deliberately — Traefik reads either, and `json.dumps`
cannot produce a quoting bug in a hostname the way hand-written YAML can.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

#: Traefik names a provider's objects `<name>@<provider>`. Deployment services
#: are created by the Docker provider from container labels, so the file
#: provider refers to them across that boundary with this suffix.
DOCKER_PROVIDER = "@docker"


@dataclass(frozen=True, slots=True)
class ProductionRoute:
    project_slug: str
    #: The router name of the deployment currently serving production. This is
    #: the deployment's `short_id`, which is also its Traefik router/service
    #: name in the Docker provider.
    service: str
    primary_host: str
    #: Every other verified domain. These redirect to the primary rather than
    #: serving the same content, because two hostnames serving identical pages
    #: is a duplicate-content problem, and this platform's owner sells the
    #: tool that reports it.
    aliases: tuple[str, ...] = ()


def config_path(directory: Path, project_slug: str) -> Path:
    return directory / f"project-{project_slug}.json"


def write_route(
    route: ProductionRoute,
    *,
    directory: Path,
    cert_resolver: str,
) -> Path:
    """Point a project's domains at a deployment. Atomic."""
    directory.mkdir(parents=True, exist_ok=True)
    entrypoint = "websecure" if cert_resolver else "web"
    scheme = "https" if cert_resolver else "http"

    primary_router = f"{route.project_slug}-primary"
    routers: dict[str, dict] = {
        primary_router: {
            "rule": _host_rule([route.primary_host]),
            "entryPoints": [entrypoint],
            "service": f"{route.service}{DOCKER_PROVIDER}",
            # Above the deployment's own wildcard route, which is a plain
            # Host() match of the same length. Traefik breaks rule-length ties
            # in an order that is stable but not documented as such, and a
            # production domain silently serving through the wrong router is
            # not a thing to leave to a tie-break.
            "priority": 100,
        }
    }
    middlewares: dict[str, dict] = {}

    if route.aliases:
        canonical = f"{route.project_slug}-canonical"
        middlewares[canonical] = {
            "redirectRegex": {
                "regex": "^https?://[^/]+(/.*)?$",
                "replacement": f"{scheme}://{route.primary_host}${{1}}",
                "permanent": True,
            }
        }
        routers[f"{route.project_slug}-aliases"] = {
            "rule": _host_rule(list(route.aliases)),
            "entryPoints": [entrypoint],
            "service": f"{route.service}{DOCKER_PROVIDER}",
            "middlewares": [canonical],
            "priority": 100,
        }

    if cert_resolver:
        for router in routers.values():
            router["tls"] = {"certResolver": cert_resolver}

    document: dict[str, dict] = {"http": {"routers": routers}}
    if middlewares:
        document["http"]["middlewares"] = middlewares

    return _write_atomic(config_path(directory, route.project_slug), document)


def clear_route(project_slug: str, *, directory: Path) -> None:
    """Remove a project's production routing.

    Used when a project is deleted, and when its last verified domain is
    removed. The deployment keeps serving on its own hostname; it simply stops
    answering for the site's domains.
    """
    config_path(directory, project_slug).unlink(missing_ok=True)


def _host_rule(hosts: list[str]) -> str:
    return " || ".join(f"Host(`{host}`)" for host in sorted(hosts))


def _write_atomic(path: Path, document: dict) -> Path:
    """Write via a temporary file in the same directory, then rename.

    Traefik watches the directory and reloads on any change. Writing in place
    would give it a window in which the file is half a config, and Traefik
    responds to a config it cannot parse by keeping the last good one and
    logging — so the promotion would appear to succeed and change nothing.
    """
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path
