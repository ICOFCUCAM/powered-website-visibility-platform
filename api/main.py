"""FastAPI application.

Composition root: this is the only module that knows about both the adapters
and the routers. The routers depend on `api.deps`, which depends on adapters;
nothing runs the other way.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.adapters import db
from api.config import get_settings
from api.domain.errors import AppError
from api.hub import deps as hub_deps
from api.hub.routes import connections as hub_connections
from api.hub.routes import oauth as hub_oauth
from api.hub.routes import properties as hub_properties
from api.hub.routes import sync as hub_sync
from api.routers import auth, health, performance, websites

logger = logging.getLogger("visibility_hub")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await db.open_pool(
        settings.database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        service_dsn=settings.service_database_url,
    )
    try:
        yield
    finally:
        await hub_deps.close_clients()
        await db.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Visibility Hub API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        # Logged without the message parameters: details can carry an email or
        # a URL, and the logging rule (spec s39) is that nothing sensitive goes
        # to the log.
        logger.info("app_error code=%s status=%s", exc.code, exc.status)
        return JSONResponse(status_code=exc.status, content=exc.as_payload())

    for router in (
        health.router,
        auth.router,
        websites.router,
        performance.router,
        # The Hub mounts its own routers. The core never imports them for
        # anything but composition, and the Hub imports none of the core's.
        hub_oauth.router,
        hub_connections.router,
        hub_properties.router,
        hub_sync.router,
    ):
        app.include_router(router, prefix="/api/v1")

    return app


app = create_app()
