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

from api.account import routes as account_routes
from api.adapters import cache, db
from api.config import get_settings
from api.domain.errors import AppError
from api.hub import deps as hub_deps
from api.hub.routes import analytics as hub_analytics
from api.hub.routes import connections as hub_connections
from api.hub.routes import oauth as hub_oauth
from api.hub.routes import properties as hub_properties
from api.hub.routes import sync as hub_sync
from api.peek import routes as peek_routes
from api.routers import (
    audit,
    auth,
    crawls,
    dashboard,
    health,
    performance,
    plans,
    reports,
    strategist,
    websites,
)

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
        await cache.check_reachable(settings.redis_url)
    except Exception:
        await db.close_pool()
        raise
    try:
        yield
    finally:
        await hub_deps.close_clients()
        await db.close_pool()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Visibility Hub API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        # The session is a bearer token the front end holds and sends as a
        # header; nothing here reads a cookie. Turning credentials off keeps
        # a browser from attaching one to a cross-origin call, and means an
        # origin list that is wrong fails visibly rather than half-working.
        # Move to cookie sessions and this becomes True — and then the origin
        # list is the only thing standing between a customer's session and
        # any site that can get them to load a page.
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
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
        crawls.router,
        audit.router,
        dashboard.router,
        plans.router,
        reports.router,
        strategist.router,
        # Account deletion spans the Hub boundary — revoke Google access AND
        # delete product data — so it is a bounded module of its own, mounted
        # here for the same reason the Hub's routers are.
        account_routes.router,
        peek_routes.router,
        # The Hub mounts its own routers. The core never imports them for
        # anything but composition, and the Hub imports none of the core's.
        hub_oauth.router,
        hub_connections.router,
        hub_properties.router,
        hub_sync.router,
        hub_analytics.router,
    ):
        app.include_router(router, prefix="/api/v1")

    return app


app = create_app()
