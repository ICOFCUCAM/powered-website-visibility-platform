"""The account, and ending it (V1 spec §41, docs/02-api.md).

Lives here rather than in `api/routers/` because deleting an account is the
one operation that genuinely spans both sides of the Hub boundary: it has to
revoke the customer's Google access AND delete the product's data. `api.hub`
may not import product logic and `api.routers` may not import the Hub, so a
core router could not do this without breaking one of the two contracts that
keep the Hub extractable. `api.account` is a bounded module of its own,
mounted by the composition root, exactly as the Hub is.

One of the two items in the definition of done that teams routinely defer past
beta. Both are compliance surface, both were promised in the M0 policy
documents that Google's review reads, and neither is retrofittable without an
awkward conversation.

Two guards on the delete, and they are the standard ones for an irreversible
action rather than anything clever:

  **The caller must type their own email address.** It is the difference
  between a mis-click and a decision, and it is the only field a hostile page
  could not supply on the customer's behalf.

  **They are told what will go first.** `GET /account` lists the websites and
  connections that are about to disappear. A dialogue that says "this cannot
  be undone" without saying what "this" is asks somebody to accept a
  consequence they cannot see.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from api.account.deletion import delete_user, summary_for
from api.account.identity import SupabaseIdentityProvider
from api.adapters import db
from api.crawler.storage import ArtifactStore, LocalArtifactStore
from api.deps import ConnectionDep, PrincipalDep
from api.domain.errors import AppError

logger = logging.getLogger("visibility_hub.account")

router = APIRouter(prefix="/account", tags=["account"])


class ConfirmationMismatch(AppError):
    code = "confirmation_mismatch"
    status = 422
    message = "Type your email address exactly to confirm."
    retriable = False


class DeleteAccount(BaseModel):
    confirm_email: str = Field(min_length=3, max_length=320)


class AccountOut(BaseModel):
    email: str
    websites: list[str]
    organizations_deleted: int
    organizations_left: int
    google_connections: int


def _store() -> ArtifactStore:
    return LocalArtifactStore(Path(os.environ.get("ARTIFACT_ROOT", "var/artifacts")))


@router.get("", response_model=AccountOut)
async def account(principal: PrincipalDep, conn: ConnectionDep) -> AccountOut:
    """What deleting this account would take with it."""
    summary = await summary_for(conn, principal.user_id)
    return AccountOut(email=principal.email, **summary)


@router.delete("", status_code=200)
async def delete_account(
    principal: PrincipalDep, body: DeleteAccount
) -> dict[str, Any]:
    """Immediate and irreversible.

    No grace period and no soft delete. A soft delete that is never hardened
    is the failure mode the launch-readiness list names — *the data-deletion
    path implemented and verified to ACTUALLY DELETE* — and a deletion that
    quietly retains everything for thirty days is not the thing the privacy
    policy describes.
    """
    if body.confirm_email.strip().lower() != principal.email.strip().lower():
        raise ConfirmationMismatch()

    # The service role, autocommit: this spans the `secrets` schema and an
    # object store, and a transaction that rolled back after the objects were
    # gone would leave the database claiming to own pages that no longer
    # exist.
    async with db.service_task() as conn:
        result = await delete_user(
            conn,
            principal.user_id,
            store=_store(),
            identity=SupabaseIdentityProvider.from_env(),
        )

    return {
        "deleted": True,
        "organizations_deleted": len(result.organizations),
        "organizations_left": len(result.organizations_left),
        "rows_deleted": result.total_rows,
        "objects_deleted": result.objects,
        "google_connections_revoked": result.tokens_revoked,
        # Surfaced, not swallowed. "Your data is gone but your sign-in still
        # works" is something a customer needs to be told.
        "google_tokens_not_revoked": result.tokens_not_revoked,
        "sign_in_deleted": result.identity_deleted,
        "sign_in_note": result.identity_reason,
        "receipt": str(result.receipt_id) if result.receipt_id else None,
    }
