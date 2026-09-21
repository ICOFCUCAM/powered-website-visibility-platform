"""Deleting an account, and meaning it.

The V1 definition of done's fourteenth item, and the one teams routinely defer
past beta: *delete their account*. It was promised in the M0 policy documents
that Google's review reads, which makes it a compliance surface as well as a
trust one, and it is not retrofittable without an awkward conversation.

WHAT "THE ACCOUNT" MEANS HERE. A person can belong to several organisations.
Deleting the person must not delete a colleague's data, and must not leave an
organisation with no owner:

  - An organisation where they are the last OWNER is deleted entirely.
  - An organisation with another owner keeps going; they are just removed.

THE CASCADE IS NOT ENOUGH, and that is the whole reason this module is longer
than one DELETE. Fourteen tenant tables cannot be reached by deleting an
organisation — the six partitioned Google fact tables, `page_snapshots`, and
the append-only logs — because a partitioned table cannot be the target of a
foreign key and the logs were built without one. A naive delete would leave
behind every search query, every impression, every page title and meta
description we ever fetched. `UNREACHABLE` names them, and a test derives the
same list from the schema so a table added later cannot quietly join them.

Three things outside Postgres also have to go: the encrypted refresh tokens in
`secrets` (the vault row is the PARENT of the connection, so deleting the
connection leaves the secret behind), the fetched HTML in object storage, and
the customer's access at Google, which is revoked rather than merely dropped.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from api.account.identity import IdentityOutcome, IdentityProvider
from api.adapters.db import fetch_all, fetch_one
from api.crawler.storage import ArtifactStore
from api.hub.services.revocation import Revocation, revoke_organisation

logger = logging.getLogger("visibility_hub.account")

#: Tenant tables that `delete from organizations` does not reach, in the order
#: they must go.
#:
#: Every one of these carries `organization_id` and no foreign key that
#: cascades — the partitioned fact tables because a partitioned table cannot
#: be referenced by one, the logs because they were built as append-only
#: records of what the system did rather than as part of the object graph.
#:
#: `alert_events` is first because its link to `alert_rules` is ON DELETE SET
#: NULL: left alone it would survive the cascade as a row pointing at nothing,
#: still carrying the organisation it belonged to.
UNREACHABLE: tuple[str, ...] = (
    "alert_events",
    "page_snapshots",
    "gsc_query_daily",
    "gsc_page_daily",
    "gsc_query_page_daily",
    "ga4_page_daily",
    "ga4_goal_daily",
    "ga4_dimension_daily",
    "llm_calls",
    "backlink_changes",
    "audit_log",
)


@dataclass
class Deletion:
    user_id: UUID
    organizations: list[UUID] = field(default_factory=list)
    #: Organisations the person left rather than deleted, because somebody
    #: else still owns them.
    organizations_left: list[UUID] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)
    objects: int = 0
    tokens_revoked: int = 0
    tokens_not_revoked: int = 0
    #: Whether the sign-in was deleted too. An account deletion that leaves
    #: the login working has not deleted the account, and if we could not do
    #: it the customer is told rather than left to find out.
    identity_deleted: bool = False
    identity_reason: str | None = None
    receipt_id: UUID | None = None

    def count(self, table: str, n: int) -> None:
        if n:
            self.rows[table] = self.rows.get(table, 0) + n

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())


async def sole_owner_organizations(
    conn: AsyncConnection, user_id: UUID
) -> tuple[list[UUID], list[UUID]]:
    """(delete these, leave these).

    "Sole owner" rather than "only member": an organisation with a viewer and
    one owner still has nobody who could take it over, so deleting the owner
    deletes it. An organisation with two owners survives.
    """
    rows = await fetch_all(
        conn,
        """
        select m.organization_id,
               (select count(*) from organization_members other
                 where other.organization_id = m.organization_id
                   and other.user_id <> m.user_id
                   and other.role = 'owner') as other_owners
          from organization_members m
         where m.user_id = %s
        """,
        (user_id,),
    )
    delete = [r["organization_id"] for r in rows if r["other_owners"] == 0]
    leave = [r["organization_id"] for r in rows if r["other_owners"] > 0]
    return delete, leave


async def _delete_objects(
    conn: AsyncConnection, organization_id: UUID, store: ArtifactStore | None
) -> int:
    """Raw artifacts, by prefix.

    The key layout is `{website_id}/...` precisely so this is possible: an
    object store has no foreign keys, and the prefix is the only handle on
    "everything we fetched for this customer".
    """
    if store is None:
        return 0
    websites = await fetch_all(
        conn, "select id from websites where organization_id = %s", (organization_id,)
    )
    removed = 0
    for row in websites:
        removed += await store.delete_prefix(f"{row['id']}/")
        removed += await store.delete_prefix(f"reports/{row['id']}/")
    return removed


async def delete_organization(
    conn: AsyncConnection,
    organization_id: UUID,
    *,
    store: ArtifactStore | None = None,
    revoke: bool = True,
    result: Deletion | None = None,
) -> Deletion:
    """Everything belonging to one organisation, in the order it must go."""
    result = result or Deletion(user_id=organization_id)

    # 1. Google first, while the connections still exist to be revoked.
    #
    # Always through the Hub, even when there is nothing to revoke: the vault
    # row is the PARENT of the connection, so deleting the connection would
    # leave the encrypted refresh token sitting in `secrets` forever — and
    # exactly one module is allowed to know how to reach that table.
    revocation: Revocation = await revoke_organisation(
        conn, organization_id, revoke_at_google=revoke
    )
    result.tokens_revoked += revocation.revoked
    result.tokens_not_revoked += revocation.not_revoked

    # 2. Object storage, while the website rows still name the prefixes.
    result.objects += await _delete_objects(conn, organization_id, store)

    # 3. The tables no cascade reaches.
    for table in UNREACHABLE:
        deleted = await fetch_one(
            conn,
            f"with gone as (delete from {table} where organization_id = %s"
            "  returning 1) select count(*) as n from gone",
            (organization_id,),
        )
        result.count(table, int(deleted["n"]) if deleted else 0)

    # 4. The organisation, which cascades the rest.
    gone = await fetch_one(
        conn,
        "with gone as (delete from organizations where id = %s returning 1)"
        " select count(*) as n from gone",
        (organization_id,),
    )
    if gone and gone["n"]:
        result.count("organizations", int(gone["n"]))
        result.organizations.append(organization_id)
    return result


async def delete_user(
    conn: AsyncConnection,
    user_id: UUID,
    *,
    store: ArtifactStore | None = None,
    revoke: bool = True,
    identity: IdentityProvider | None = None,
) -> Deletion:
    """The whole path, and then the receipt.

    The connection must be the SERVICE role and must be AUTOCOMMIT: this
    spans the `secrets` schema and an object store, and a transaction that
    rolled back after the objects were gone would leave the database claiming
    to own pages that no longer exist.
    """
    result = Deletion(user_id=user_id)
    to_delete, to_leave = await sole_owner_organizations(conn, user_id)
    result.organizations_left = list(to_leave)

    for organization_id in to_delete:
        await delete_organization(
            conn, organization_id, store=store, revoke=revoke, result=result
        )

    # Memberships of organisations somebody else owns go with the user row
    # (organization_members cascades from users); everything else that
    # referenced them is ON DELETE SET NULL, so a colleague's audit history
    # keeps its shape and loses only the name of who dismissed an issue.
    gone = await fetch_one(
        conn,
        "with gone as (delete from users where id = %s returning 1)"
        " select count(*) as n from gone",
        (user_id,),
    )
    result.count("users", int(gone["n"]) if gone else 0)

    # Last, because it is the one step that is not ours to roll back and the
    # one whose failure must not stop the data deletion.
    outcome: IdentityOutcome = (
        await identity.delete(user_id)
        if identity is not None
        else IdentityOutcome(deleted=False, reason="no identity provider is configured")
    )
    result.identity_deleted = outcome.deleted
    result.identity_reason = outcome.reason

    receipt = await fetch_one(
        conn,
        """
        insert into deletion_receipts
            (user_id, organization_ids, rows_deleted, objects_deleted,
             tokens_revoked, tokens_not_revoked, identity_deleted, completed_at)
        values (%s, %s, %s, %s, %s, %s, %s, now())
        returning id
        """,
        (
            user_id,
            result.organizations,
            json.dumps(result.rows),
            result.objects,
            result.tokens_revoked,
            result.tokens_not_revoked,
            result.identity_deleted,
        ),
    )
    result.receipt_id = receipt["id"] if receipt else None

    logger.info(
        "deleted account: organisations=%d rows=%d objects=%d "
        "tokens_revoked=%d tokens_not_revoked=%d identity_deleted=%s receipt=%s",
        len(result.organizations), result.total_rows, result.objects,
        result.tokens_revoked, result.tokens_not_revoked,
        result.identity_deleted, result.receipt_id,
    )
    return result


async def summary_for(conn: AsyncConnection, user_id: UUID) -> dict[str, Any]:
    """What a person is about to lose, so the confirmation is informed.

    A dialogue that says "this cannot be undone" without saying what "this"
    is asks somebody to accept a consequence they cannot see.
    """
    to_delete, to_leave = await sole_owner_organizations(conn, user_id)
    websites = await fetch_all(
        conn,
        "select w.domain from websites w where w.organization_id = any(%s)"
        " order by w.domain",
        (to_delete,),
    )
    connections = await fetch_one(
        conn,
        "select count(*) as n from connections where organization_id = any(%s)"
        "   and status = 'active'",
        (to_delete,),
    )
    return {
        "websites": [str(row["domain"]) for row in websites],
        "organizations_deleted": len(to_delete),
        "organizations_left": len(to_leave),
        "google_connections": int(connections["n"]) if connections else 0,
    }
