-- 0009_provenance.sql
--
-- THE PROVENANCE CONTRACT
--
-- Six months from now someone will point at a number on the dashboard and ask
-- where it came from. With Google data, crawler data, vendor data, modelled
-- estimates, calculated scores and AI-written explanations all in one view,
-- that question is unanswerable unless every derived value carries its origin.
--
-- So every table holding a DERIVED number carries the same five columns:
--
--   source              which class of input produced it
--   derived_from        the specific records, by id or key
--   observed_from/_to   the window of source data it summarises
--   computed_at         when this value was calculated
--   calculation_version which version of the rule/score/prompt produced it
--
-- `calculation_version` is what makes a DETERMINISTIC number reproducible:
-- the same inputs under the same version yield the same output, and a version
-- bump is the only legitimate reason for such a value to change.
--
-- Model-dependent output is not reproducible and does not pretend to be. See
-- migration 0011, which splits the two contracts.

create type provenance_source as enum (
    'search_console',   -- Google-originated measurement
    'analytics',        -- Google-originated measurement
    'crawl',            -- our own observation of the open web
    'vendor',           -- licensed third-party data
    'modelled',         -- computed estimate, never a measurement
    'derived',          -- computed from other rows in this database
    'user'              -- stated by the customer
);

-- ---------------------------------------------------------------------------
-- Scores
-- ---------------------------------------------------------------------------
alter table score_snapshots
    add column source            provenance_source not null default 'derived',
    add column derived_from      jsonb not null default '{}'::jsonb,
    add column observed_from     date,
    add column observed_to       date;
-- scoring_version is this table's calculation_version; computed_at exists.
comment on column score_snapshots.scoring_version is
    'calculation_version for this table. Changing it requires recomputing every historical snapshot.';

-- ---------------------------------------------------------------------------
-- Issues: which rule, at which version, over which evidence
-- ---------------------------------------------------------------------------
alter table issue_types
    add column rule_version text not null default '1.0.0';

alter table issues
    add column source              provenance_source not null default 'derived',
    add column derived_from        jsonb not null default '{}'::jsonb,
    add column observed_from       date,
    add column observed_to         date,
    add column computed_at         timestamptz not null default now(),
    add column calculation_version text not null default '1.0.0';

-- ---------------------------------------------------------------------------
-- Recommendations and plans
-- ---------------------------------------------------------------------------
alter table recommendations
    add column source              provenance_source not null default 'derived',
    add column derived_from        jsonb not null default '{}'::jsonb,
    add column observed_from       date,
    add column observed_to         date,
    add column computed_at         timestamptz not null default now(),
    add column calculation_version text not null default '1.0.0';

alter table plans
    add column source          provenance_source not null default 'derived',
    add column derived_from    jsonb not null default '{}'::jsonb,
    add column observed_from   date,
    add column observed_to     date;

-- ---------------------------------------------------------------------------
-- Vendor and modelled values already carried provider_key / is_modelled /
-- as_of. Align them to the same vocabulary so one query answers "where did
-- this come from" across every derived table.
-- ---------------------------------------------------------------------------
alter table external_metrics
    add column source              provenance_source not null default 'vendor',
    add column derived_from        jsonb not null default '{}'::jsonb,
    add column computed_at         timestamptz not null default now(),
    add column calculation_version text not null default '1.0.0';

-- ---------------------------------------------------------------------------
-- AI output is an explanation OF evidence, never evidence itself. Recording
-- which records a generation was grounded in is what lets a disputed sentence
-- be traced back to the rows that produced it.
-- ---------------------------------------------------------------------------
alter table llm_calls
    add column derived_from jsonb not null default '{}'::jsonb;

-- ---------------------------------------------------------------------------
-- One view answering the provenance question across every derived table.
-- ---------------------------------------------------------------------------
create view provenance_index as
select 'score_snapshots' as table_name, id::text as record_id, website_id,
       source, derived_from, observed_from, observed_to, computed_at,
       scoring_version as calculation_version
from score_snapshots
union all
select 'issues', id::text, website_id, source, derived_from,
       observed_from, observed_to, computed_at, calculation_version
from issues
union all
select 'recommendations', id::text, website_id, source, derived_from,
       observed_from, observed_to, computed_at, calculation_version
from recommendations
union all
select 'external_metrics', id::text, website_id, source, derived_from,
       as_of, as_of, computed_at, calculation_version
from external_metrics;
