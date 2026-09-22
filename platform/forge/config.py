"""Runtime configuration, read once from the environment.

Same rule as the sibling project: no secret has a default. A missing secret
fails at startup rather than silently degrading to an insecure mode at request
time — which for this service would mean deploying containers with an empty
encryption key and writing unencrypted environment variables to disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is required but not set")
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str

    #: Fernet key protecting every stored environment variable. Rotating it
    #: without re-encrypting makes every existing variable unreadable, which is
    #: why it is named a master key rather than a password.
    master_key: str

    #: Bearer token for the control plane API. One user, one token — the
    #: audience decision from day one, and the reason there is no login screen.
    api_token: str

    #: Every deployment gets `<short-id>.<deploy_domain>` for as long as it
    #: exists. A wildcard DNS record and a wildcard certificate cover all of
    #: them at once, so a new deployment needs no DNS work and no ACME round
    #: trip — it is addressable the moment its container is up.
    deploy_domain: str

    #: The Docker network the router and every deployment share. The router
    #: reaches containers on it by name, so no deployment ever publishes a
    #: port on the host.
    network: str

    #: Where repositories are cloned and build contexts are assembled. Must be
    #: writable, and on the same filesystem Docker builds from.
    build_root: Path

    #: The directory the edge router watches for dynamic configuration. Every
    #: promotion writes one file here. Shared with the router container, which
    #: only ever reads it.
    router_config_dir: Path

    #: Name of the Traefik certificate resolver to attach to public routes.
    #: Empty disables TLS entirely, which is what local development wants and
    #: what production must never have.
    cert_resolver: str

    #: A build that has not finished by now is not going to. Kills the builder
    #: and fails the deployment rather than holding the worker forever.
    build_timeout_seconds: int

    #: How long a freshly started container has to answer its health check
    #: before the deployment is declared failed and the container destroyed.
    health_timeout_seconds: int
    health_path: str

    environment: str
    pool_min_size: int
    pool_max_size: int

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def tls_enabled(self) -> bool:
        return bool(self.cert_resolver)

    @property
    def scheme(self) -> str:
        return "https" if self.tls_enabled else "http"

    def deployment_host(self, short_id: str) -> str:
        return f"{short_id}.{self.deploy_domain}"

    def deployment_url(self, short_id: str) -> str:
        return f"{self.scheme}://{self.deployment_host(short_id)}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    environment = _optional("ENVIRONMENT", "development")
    cert_resolver = _optional("FORGE_CERT_RESOLVER", "")

    if environment == "production" and not cert_resolver:
        # Serving customer sites over plaintext is not a degraded mode worth
        # having. In development it is the only sane default; in production it
        # is a misconfiguration that should never reach a request.
        raise ConfigError(
            "FORGE_CERT_RESOLVER must be set in production — "
            "refusing to serve deployments without TLS"
        )

    return Settings(
        database_url=_required("DATABASE_URL"),
        master_key=_required("FORGE_MASTER_KEY"),
        api_token=_required("FORGE_API_TOKEN"),
        deploy_domain=_required("FORGE_DEPLOY_DOMAIN").lower().lstrip("*.").strip("."),
        network=_optional("FORGE_NETWORK", "forge"),
        build_root=Path(_optional("FORGE_BUILD_ROOT", "/var/lib/forge/builds")),
        router_config_dir=Path(
            _optional("FORGE_ROUTER_CONFIG_DIR", "/var/lib/forge/router")
        ),
        cert_resolver=cert_resolver,
        build_timeout_seconds=_int("FORGE_BUILD_TIMEOUT", 1800),
        health_timeout_seconds=_int("FORGE_HEALTH_TIMEOUT", 90),
        health_path=_optional("FORGE_HEALTH_PATH", "/"),
        environment=environment,
        pool_min_size=_int("FORGE_POOL_MIN", 1),
        pool_max_size=_int("FORGE_POOL_MAX", 8),
    )
