-- 0022_alerting.sql
--
-- Telling somebody when things break.
--
-- Two audiences, and conflating them is the main way alerting goes wrong:
--
--   OPERATOR   "the machine is broken" — a job failing across tenants, the
--              scheduler not dispatching, a pool that stopped. Goes to us.
--              Not tenant data, so it lives outside the tenancy graph.
--
--   CUSTOMER   "something is wrong with YOUR account that only you can fix" —
--              a Google connection that needs reconnecting, a site we can no
--              longer crawl. Goes to them, through `alert_events`, which was
--              built for it.
--
-- THE FAILURE MODE OF ALERTING IS NOISE. A nightly job that fails for four
-- hundred websites must produce one alert, not four hundred, and it must not
-- produce that one alert again every five minutes for a week. So an operator
-- alert is an INCIDENT keyed on a fingerprint, not a log line: opened once,
-- counted while it persists, re-notified on a schedule, and resolved — with a
-- recovery notice — when the scan stops seeing it.
--
-- The opposite failure is just as bad. An alert that fires once and then goes
-- quiet while the problem continues is worse than one that repeats, because
-- silence reads as recovery. `occurrences` and `last_seen_at` are what let a
-- human tell "it happened once at 3am" from "it has been happening all week".

create table operator_alerts (
    id              bigserial primary key,

    -- One open incident per fingerprint. `sha256(kind:subject)` — stable
    -- across scans, so the second sighting updates the first rather than
    -- opening a second incident.
    fingerprint     text not null,
    kind            text not null,
    severity        text not null check (severity in ('warning', 'critical')),
    title           text not null,
    detail          jsonb not null default '{}'::jsonb,

    occurrences     int not null default 1,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),

    -- Null until somebody has been told. Separate from first_seen_at because
    -- "we noticed" and "we told someone" are different facts, and a delivery
    -- that failed must not look like one that happened.
    notified_at     timestamptz,
    notify_count    int not null default 0,

    resolved_at     timestamptz,
    -- Whether the recovery notice went out, so a resolution is not announced
    -- twice and is not silently skipped.
    resolved_notified_at timestamptz
);

-- The claim: only one OPEN incident per fingerprint. A resolved one stays for
-- history, which is why the index is partial rather than a plain unique.
create unique index operator_alerts_open on operator_alerts (fingerprint)
    where resolved_at is null;
create index on operator_alerts (resolved_at, last_seen_at desc);

comment on table operator_alerts is
    'Operator incidents, keyed on a fingerprint. One open row per problem, counted while it persists — not one row per sighting.';

-- Outside the tenancy graph and operator-only, the same shape and for the same
-- reason as deletion_receipts: it describes the fleet, not a customer, and
-- `roles.sql` would silently undo a REVOKE on its next run.
alter table operator_alerts enable row level security;
create policy operator_alerts_operator_only on operator_alerts
    for select using (false);

-- ---------------------------------------------------------------------------
-- Customer alerts
-- ---------------------------------------------------------------------------
-- `alert_events` was built for phase 3's threshold rules and has no `kind`,
-- which is what a fingerprint needs. A V1 notice has no rule behind it —
-- nobody configured "tell me when my Google connection dies", it is simply
-- true that they need to know — so `rule_id` stays null and `kind` carries
-- the meaning.
alter table alert_events
    add column kind text not null default 'unspecified',
    -- Why a delivery did not happen. Null with a null delivered_at means it
    -- has not been tried yet; set means it was tried and failed.
    add column delivery_error text;

create index on alert_events (website_id, kind, fired_at desc);

comment on column alert_events.kind is
    'What happened, for fingerprinting and suppression. rule_id is null for notices nobody had to configure.';
