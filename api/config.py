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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    jwt_secret = _required("JWT_SECRET")
    # HS256 keys shorter than the hash output weaken the signature (RFC 7518
    # s3.2). Fail at startup rather than warn once per token.
    if len(jwt_secret.encode()) < 32:
        raise ConfigError("JWT_SECRET must be at least 32 bytes")

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
        environment=os.environ.get("ENVIRONMENT", "development"),
        pool_min_size=int(os.environ.get("DB_POOL_MIN", "1")),
        pool_max_size=int(os.environ.get("DB_POOL_MAX", "10")),
        web_base_url=os.environ.get("WEB_BASE_URL", "http://localhost:3000"),
    )
