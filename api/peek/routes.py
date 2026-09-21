"""The public scan endpoint.

Mounted from the composition root rather than living with the authenticated
routers, because it is the one route in the product that serves somebody with
no account — and that difference should be visible in the file tree rather
than buried in a decorator.

Nothing here reads or writes tenant data. It takes a URL, fetches one page
through the safety guard, runs the deterministic rules over it, and returns
what they said.
"""

from __future__ import annotations

import logging
import os

import redis.asyncio as redis
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.crawler.safety import UnsafeUrl
from api.peek import limits, service

logger = logging.getLogger("visibility_hub.peek")

router = APIRouter(prefix="/peek", tags=["peek"])

_redis: redis.Redis | None = None


def _client() -> redis.Redis | None:
    global _redis
    if _redis is None:
        url = os.environ.get("REDIS_URL")
        if not url:
            return None
        _redis = redis.from_url(url, decode_responses=True)
    return _redis


class PeekIn(BaseModel):
    url: str = Field(min_length=1, max_length=300)


class PeekOut(BaseModel):
    url: str
    final_url: str
    status_code: int
    title: str | None
    findings: list[dict]
    checked: int


def _error(status: int, code: str, message: str) -> JSONResponse:
    """The product's one error envelope. A public endpoint inventing its own
    shape would mean the web client needs two parsers."""
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": {},
                "retriable": status in (429, 502),
            }
        },
    )


def _caller(request: Request) -> str:
    """The visitor's address, trusting the proxy only for its nearest hop."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


@router.post("", response_model=PeekOut)
async def peek(body: PeekIn, request: Request):
    try:
        await limits.claim(_client(), _caller(request))
    except limits.TooMany as exc:
        return _error(429, "too_many_scans", str(exc))

    try:
        result = await service.run(body.url)
    except service.PeekFailed as exc:
        return _error(400, "peek_failed", str(exc))
    except UnsafeUrl as exc:
        # Deliberately the same shape as any other bad address: an error that
        # distinguishes "private address" from "does not resolve" is a port
        # scanner with a nice interface.
        logger.info("peek refused an address: %s", exc)
        return _error(400, "peek_refused", "We can't scan that address.")
    except Exception:
        logger.exception("peek failed")
        return _error(502, "peek_unreachable", "We couldn't reach that site.")

    return PeekOut(
        url=result.url,
        final_url=result.final_url,
        status_code=result.status_code,
        title=result.title,
        findings=result.findings,
        checked=result.checked,
    )
