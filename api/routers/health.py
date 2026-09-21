from __future__ import annotations

from fastapi import APIRouter

from api.adapters import db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    async with db.session() as conn:
        await conn.execute("select 1")
    return {"status": "ok"}
