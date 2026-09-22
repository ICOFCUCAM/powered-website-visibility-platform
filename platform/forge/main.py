"""FastAPI application.

Composition root: the only module that knows about both the adapters and the
routers. Routers depend on `forge.deps`, which depends on adapters; nothing
runs the other way.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from forge.adapters import db
from forge.config import get_settings
from forge.domain.errors import ForgeError
from forge.routers import deployments, health, processes, projects, webhooks
from forge.web import routes as dashboard

logger = logging.getLogger("forge")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await db.open_pool(
        settings.database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
    )
    # Created here as well as by the worker: the API writes router config the
    # moment a domain is verified, and a missing directory would make that the
    # first thing to fail rather than something noticed at startup.
    settings.router_config_dir.mkdir(parents=True, exist_ok=True)
    settings.build_root.mkdir(parents=True, exist_ok=True)
    logger.info(
        "forge control plane ready — deployments at *.%s over %s",
        settings.deploy_domain,
        settings.scheme,
    )
    try:
        yield
    finally:
        await db.close_pool()


app = FastAPI(
    title="Forge",
    version="0.1.0",
    summary="A self-hosted deployment platform",
    lifespan=lifespan,
)

app.mount(
    "/static",
    StaticFiles(directory=str(dashboard.HERE / "static")),
    name="static",
)

app.include_router(health.router)
app.include_router(projects.router)
app.include_router(deployments.project_router)
app.include_router(deployments.router)
app.include_router(processes.project_router)
app.include_router(processes.router)
app.include_router(webhooks.router)
# Last, because it owns the root path and its routes are the least specific.
app.include_router(dashboard.router)


@app.exception_handler(dashboard.NeedsLogin)
async def handle_needs_login(request: Request, exc: Exception) -> RedirectResponse:
    """A browser gets the sign-in page; only the API gets a 401.

    Sending JSON to someone who typed a URL is the kind of thing that makes a
    dashboard feel broken when it is merely locked.
    """
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(ForgeError)
async def handle_forge_error(request: Request, exc: ForgeError) -> JSONResponse:
    """Every deliberate error becomes its own status code and a message
    written for the person reading it, not a stack trace."""
    if exc.status_code >= 500:
        logger.exception("unhandled: %s", exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )
