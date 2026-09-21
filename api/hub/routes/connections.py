"""Connected accounts (V1 spec s30, s37)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, status
from fastapi.responses import Response
from pydantic import BaseModel

from api.deps import MembershipsDep
from api.domain.errors import NotFound
from api.hub.deps import GoogleClientDep, ServiceConnectionDep, TokenVaultDep
from api.hub.repositories import HubRepository
from api.hub.services import events

router = APIRouter(prefix="/google", tags=["google"])


class ConnectionOut(BaseModel):
    id: str
    provider: str
    account: str
    granted_scopes: list[str]
    status: str
    last_error: str | None
    connected_at: datetime
    last_refreshed_at: datetime | None

    @property
    def needs_reauth(self) -> bool:
        return self.status == "needs_reauth"


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(
    memberships: MembershipsDep, conn: ServiceConnectionDep
) -> list[ConnectionOut]:
    repo = HubRepository(conn)
    out: list[ConnectionOut] = []
    for membership in memberships:
        for row in await repo.list_connections(membership.organization_id):
            out.append(
                ConnectionOut(
                    id=str(row["id"]),
                    provider=row["provider_key"],
                    account=row["label"],
                    granted_scopes=row["granted_scopes"],
                    status=row["status"],
                    last_error=row["last_error"],
                    connected_at=row["created_at"],
                    last_refreshed_at=row["last_refreshed_at"],
                )
            )
    return out


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    connection_id: Annotated[UUID, Path()],
    memberships: MembershipsDep,
    conn: ServiceConnectionDep,
    vault: TokenVaultDep,
    client: GoogleClientDep,
) -> Response:
    """Disconnect must actually disconnect (V1 spec s37).

    Revoke with Google, destroy the stored secret, mark the connection revoked.
    Historical data already synced is retained per the retention policy — and
    the UI says so rather than leaving the user to guess.
    """
    repo = HubRepository(conn)
    record = await repo.get_connection(connection_id)

    # This route runs on the service connection, which bypasses RLS, so the
    # tenant check is explicit and mandatory.
    owned = {m.organization_id for m in memberships}
    if record is None or record["organization_id"] not in owned:
        raise NotFound("We couldn't find that connection.")

    if record["refresh_token_id"]:
        token = await vault.read(record["refresh_token_id"])
        if token:
            # Best effort: if Google already considers it dead, that is the
            # outcome we wanted anyway.
            await client.revoke(token)
        await vault.destroy(record["refresh_token_id"])

    await repo.mark_revoked(connection_id)
    await events.publish(
        events.DomainEvent(
            events.CONNECTION_REVOKED,
            {
                "connection_id": str(connection_id),
                "organization_id": str(record["organization_id"]),
            },
        )
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
