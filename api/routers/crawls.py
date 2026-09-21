"""Crawl admission and progress (V1 spec s30).

Admission is one of the two gates on decision 20. The crawler re-checks the
same prerequisites before it fetches, because a queue entry must never outlive
the permission that created it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, status
from pydantic import BaseModel

from api.deps import ConnectionDep, WebsiteRepoDep, WebsiteScopeDep
from api.domain.crawl_policy import evaluate_crawl_allowed
from api.domain.errors import AppError, CrawlNotAllowed
from api.repositories.postgres.crawls import CrawlRepository

router = APIRouter(prefix="/websites", tags=["crawls"])


class CrawlInProgress(AppError):
    code = "crawl_in_progress"
    status = 409
    message = "A crawl is already running for this website."
    retriable = True


class CrawlOut(BaseModel):
    id: str
    status: str
    trigger: str
    pages_discovered: int
    pages_fetched: int
    pages_rendered: int
    fetch_errors: int
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_summary: dict | None


@router.post("/{website_id}/crawl", response_model=CrawlOut,
             status_code=status.HTTP_202_ACCEPTED)
async def start_crawl(
    scope: WebsiteScopeDep, conn: ConnectionDep, websites: WebsiteRepoDep
) -> CrawlOut:
    scope.require_write()
    repo = CrawlRepository(conn)

    if await repo.running_for(scope.website.id):
        raise CrawlInProgress()

    covers = await websites.ownership_covers_canonical_url(scope.website.id)
    decision = evaluate_crawl_allowed(scope.website, ownership_covers_target=covers)
    if not decision.allowed:
        raise CrawlNotAllowed(details_reason=decision.reason)

    row = await repo.create(
        organization_id=scope.website.organization_id,
        website_id=scope.website.id,
        trigger="manual",
    )
    return CrawlOut(**_shape(row))


@router.get("/{website_id}/crawls", response_model=list[CrawlOut])
async def list_crawls(scope: WebsiteScopeDep, conn: ConnectionDep) -> list[CrawlOut]:
    rows = await CrawlRepository(conn).list_for(scope.website.id)
    return [CrawlOut(**_shape(r)) for r in rows]


@router.get("/{website_id}/crawls/{crawl_id}", response_model=CrawlOut)
async def get_crawl(
    scope: WebsiteScopeDep,
    crawl_id: Annotated[UUID, Path()],
    conn: ConnectionDep,
) -> CrawlOut:
    row = await CrawlRepository(conn).get(scope.website.id, crawl_id)
    if row is None:
        from api.domain.errors import NotFound

        raise NotFound("We couldn't find that crawl.")
    return CrawlOut(**_shape(row))


def _shape(row) -> dict:
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "trigger": row["trigger"],
        "pages_discovered": row["pages_discovered"],
        "pages_fetched": row["pages_fetched"],
        "pages_rendered": row["pages_rendered"],
        "fetch_errors": row["fetch_errors"],
        "queued_at": row["queued_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error_summary": row["error_summary"],
    }
