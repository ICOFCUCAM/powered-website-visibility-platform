"""Runtime configuration, read once from the environment.

No secret has a default. A missing secret fails at startup rather than
silently degrading to an insecure mode at request time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is required but not set")
    return value


@dataclass(frozen=True, slots=True)
class GoogleSettings:
    """Only required when the Hub is actually used.

    Kept out of Settings so a developer can run the API, the crawler and the
    tests without Google credentials — and so a missing credential produces a
    clear error at the point of use rather than a startup failure that looks
    unrelated.
    """

    client_id: str
    client_secret: str
    redirect_uri: str
    token_master_key: str
    redis_url: str
    web_base_url: str


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    service_database_url: str
    jwt_secret: str
    jwt_algorithm: str
    jwt_audience: str | None
    environment: str
    pool_min_size: int
    pool_max_size: int
    #: Where the customer-facing app lives. Used to build the links in the
    #: weekly email, which is the one place the API has to know its own
    #: front end's address.
    web_base_url: str
    #: Which browser origins may call this API. In any real deployment the
    #: front end and the API are two different origins by construction — the
    #: app on Vercel, the API on a container host — so this is configuration,
    #: never a constant.
    cors_origins: tuple[str, ...]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def google(self) -> GoogleSettings:
        return GoogleSettings(
            client_id=_required("GOOGLE_CLIENT_ID"),
            client_secret=_required("GOOGLE_CLIENT_SECRET"),
            redirect_uri=_required("GOOGLE_REDIRECT_URI"),
            token_master_key=_required("TOKEN_MASTER_KEY"),
            redis_url=_required("REDIS_URL"),
            web_base_url=os.environ.get("WEB_BASE_URL", "http://localhost:3000"),
        )


def _cors_origins(environment: str) -> tuple[str, ...]:
    """Who may call this API from a browser.

    Production has no default, for the same reason no secret has one. The
    front end and the API are two origins in any real deployment, so a
    localhost fallback would not be a degraded mode — it would be a deployment
    where every request is blocked, with the error appearing in the customer's
    browser console rather than in our logs at startup.

    `*` is refused rather than passed through. It is the setting somebody
    reaches for when CORS is failing and the deadline is close, and it makes
    every website on the internet able to call this API with a stolen token.
    """
    raw = os.environ.get("CORS_ORIGINS", "")
    origins = tuple(
        origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()
    )

    if environment != "production":
        return origins or ("http://localhost:3000",)

    if not origins:
        raise ConfigError(
            "CORS_ORIGINS is required in production: set it to the front end's "
            "origin, e.g. https://app.example.com"
        )
    if "*" in origins:
        raise ConfigError(
            "CORS_ORIGINS may not be '*': every site on the internet could "
            "then call this API with a token taken from a customer's browser"
        )
    insecure = [origin for origin in origins if not origin.startswith("https://")]
    if insecure:
        raise ConfigError(
            f"CORS_ORIGINS must be https in production: {', '.join(insecure)}"
        )
    return origins


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    jwt_secret = _required("JWT_SECRET")
    # HS256 keys shorter than the hash output weaken the signature (RFC 7518
    # s3.2). Fail at startup rather than warn once per token.
    if len(jwt_secret.encode()) < 32:
        raise ConfigError("JWT_SECRET must be at least 32 bytes")

    environment = os.environ.get("ENVIRONMENT", "development")

    return Settings(
        database_url=_required("DATABASE_URL"),
        # Falls back to the request-path DSN so a developer who has not set it
        # gets a clear failure from the vault rather than a silent superuser
        # connection.
        service_database_url=os.environ.get("SERVICE_DATABASE_URL")
        or _required("DATABASE_URL"),
        jwt_secret=jwt_secret,
        jwt_algorithm=os.environ.get("JWT_ALGORITHM", "HS256"),
        jwt_audience=os.environ.get("JWT_AUDIENCE") or None,
        environment=environment,
        pool_min_size=int(os.environ.get("DB_POOL_MIN", "1")),
        pool_max_size=int(os.environ.get("DB_POOL_MAX", "10")),
        web_base_url=os.environ.get("WEB_BASE_URL", "http://localhost:3000"),
        cors_origins=_cors_origins(environment),
    )
