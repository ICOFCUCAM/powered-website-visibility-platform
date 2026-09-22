"""Liveness, and enough of a readiness check to be worth wiring to a monitor."""

from __future__ import annotations

from fastapi import APIRouter

from forge.adapters import db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    """Answers only if the database answers.

    A control plane that cannot reach Postgres cannot queue, promote or report
    anything, so reporting itself ready would be a lie that a load balancer
    would believe.
    """
    async with db.connection() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ready", "database": "ok"}
