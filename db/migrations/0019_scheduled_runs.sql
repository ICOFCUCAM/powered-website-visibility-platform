-- 0019_scheduled_runs.sql
--
-- The nightly schedule's record of what it did, and — more importantly — its
-- CLAIM on what it is about to do.
--
-- The unique index is the lock. A dispatcher that wants to run tonight's
-- Search Console sync for a website inserts the row for that slot; if the
-- insert conflicts, some other dispatcher already owns it and this one does
-- nothing. No advisory lock, no Redis key, no leader election — the same
-- discipline as `crawl_frontier`, where the database is the only thing that
-- has to be right.
--
-- That also gives the property this schedule needs most: A MISSED WINDOW IS
-- LATE, NOT LOST. The dispatcher claims the most recent slot that has passed,
-- so a worker pool that was down from 01:00 to 09:00 runs last night's sync
-- at 09:05 rather than skipping to tomorrow. Exactly one slot is ever caught
-- up, so an outage of a week does not produce a week of work.
--
-- And it answers the question an operator actually asks, which is never "is
-- the cron running" but "did THIS customer's data refresh last night, and if
-- not, why not".

create table scheduled_runs (
    id              bigserial primary key,
    organization_id uuid not null references organizations(id) on delete cascade,
    website_id      uuid not null references websites(id) on delete cascade,

    job             text not null check (job in (
                        'sync_search_console', 'sync_analytics', 'crawl_website',
                        'calculate_scores', 'generate_recommendations',
                        'generate_weekly_report')),

    -- The canonical slot this claim is for: the job's base time on a given
    -- day, plus this website's stagger. Stored rather than derived so a
    -- change to the stagger function cannot silently re-open a closed window.
    window_start    timestamptz not null,

    status          text not null default 'claimed'
                         check (status in ('claimed', 'running', 'succeeded',
                                           'failed', 'skipped')),
    trigger         text not null default 'schedule'
                         check (trigger in ('schedule', 'manual', 'bootstrap')),

    claimed_at      timestamptz not null default now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    error           text,
    -- Whatever the job wants an operator to see: rows written, pages fetched,
    -- why it skipped.
    detail          jsonb not null default '{}'::jsonb,

    unique (website_id, job, window_start)
);

create index on scheduled_runs (website_id, job, window_start desc);
-- The operator's query: what is stuck.
create index on scheduled_runs (status, claimed_at)
    where status in ('claimed', 'running');

comment on table scheduled_runs is
    'One row per (website, job, slot). The unique index is the claim: inserting it is how a dispatcher wins the right to run that slot.';

-- ---------------------------------------------------------------------------
-- Readable by the organisation, written only by the system — the same shape
-- as llm_calls and for the same reason. A customer may see that Tuesday's
-- sync failed; nothing reachable from a browser may write that history.
-- ---------------------------------------------------------------------------
alter table scheduled_runs enable row level security;

create policy scheduled_runs_read on scheduled_runs for select
    using (app.is_org_member(organization_id));
