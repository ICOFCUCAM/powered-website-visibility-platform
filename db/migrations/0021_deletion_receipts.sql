-- 0021_deletion_receipts.sql
--
-- Proof that a deletion happened, containing nothing that was deleted.
--
-- The two obligations pull against each other. A customer asks to be erased
-- and we must erase them; a regulator, or the customer themselves six months
-- later, asks whether we did, and "we think so" is not an answer. So this
-- table keeps the SHAPE of the deletion — when, how many rows from which
-- tables, how many Google tokens were revoked — and none of its content. No
-- email, no name, no domain.
--
-- `user_id` and `organization_ids` are random UUIDs that no longer resolve to
-- anything. They identify the event, not the person.
--
-- Deliberately outside the tenancy graph: no foreign keys, nothing cascades
-- into it, and the row outlives everything it describes. A deletion record
-- that is itself deleted by the deletion is not a record.

create table deletion_receipts (
    id                  uuid primary key default gen_random_uuid(),
    user_id             uuid not null,
    organization_ids    uuid[] not null default '{}',

    -- {"gsc_query_daily": 40321, "page_snapshots": 812, ...}
    rows_deleted        jsonb not null default '{}'::jsonb,
    objects_deleted     int not null default 0,

    -- Revocation is best effort: if Google is down we still delete, because
    -- the customer asked us to. Recording the failure is how anyone finds out
    -- a token was destroyed without being revoked first.
    tokens_revoked      int not null default 0,
    tokens_not_revoked  int not null default 0,

    requested_at        timestamptz not null default now(),
    completed_at        timestamptz
);

create index on deletion_receipts (user_id);
create index on deletion_receipts (requested_at desc);

comment on table deletion_receipts is
    'The shape of a deletion, never its content. Outside the tenancy graph on purpose: a deletion record that the deletion deletes is not a record.';

-- ---------------------------------------------------------------------------
-- Operator-only, and it has to say so structurally.
--
-- There is no organization_id to scope a policy by — the organisation is gone
-- — so the request-path role gets a policy that matches nothing. That is not
-- the same as leaving RLS off: `roles.sql` grants every table to `app_user`
-- on every run, so a REVOKE here would be silently undone the next time
-- anyone added a table.
-- ---------------------------------------------------------------------------
alter table deletion_receipts enable row level security;

create policy deletion_receipts_operator_only on deletion_receipts
    for select using (false);

-- An account deletion that leaves the sign-in working has not deleted the
-- account. The application stores no credentials of its own, so the identity
-- lives with the auth provider — and whether we managed to delete it there is
-- part of what happened.
alter table deletion_receipts
    add column identity_deleted boolean not null default false;
