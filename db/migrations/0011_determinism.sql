-- 0011_determinism.sql
--
-- Corrects an invariant that was stated too absolutely.
--
-- WAS: "the same inputs under the same calculation_version must yield the same
--       output" — true of a scoring function, false of anything that calls a
--       model, and pretending otherwise would have had someone debugging a
--       "reproducibility bug" that is just how language models work.
--
-- NOW, two contracts, and every derived row declares which one it is under:
--
--   deterministic = true
--     Reproducible from the same inputs and calculation_version. Scores,
--     rule findings, impact rankings. A differing output is a real bug.
--
--   deterministic = false
--     Not reproducible, and not expected to be. Records instead: which
--     provider and model version produced it, which prompt version, which
--     evidence it was grounded in, and when it was generated. That is
--     ACCOUNTABILITY rather than reproducibility, and it is the honest
--     guarantee for model output.

alter table score_snapshots  add column deterministic boolean not null default true;
alter table issues           add column deterministic boolean not null default true;
alter table recommendations  add column deterministic boolean not null default true;
alter table external_metrics add column deterministic boolean not null default true;

-- A modelled estimate is never reproducible by us: it came out of someone
-- else's model. Declaring one deterministic is a category error, so the
-- database refuses it rather than letting the claim sit there looking true.
alter table external_metrics add constraint modelled_is_not_deterministic
    check (source <> 'modelled' or deterministic = false);

-- Ranking and prose live in the same recommendation row but have different
-- contracts: `impact_score` and `rank` are computed in code and reproducible;
-- `body_md` and `how_to_md` are written by a model and are not. The row's
-- `deterministic` flag describes its DERIVATION — how it came to be a
-- recommendation at all — and the generation record below covers its prose.
comment on column recommendations.deterministic is
    'Describes the derivation (ranking, impact, selection), which is code. The generated prose is covered by its ai_generations row.';

-- ---------------------------------------------------------------------------
-- Model output gets an accountability record, not a reproducibility claim.
-- llm_calls already metered cost; it now also carries the provenance of what
-- was produced and what it was attached to.
-- ---------------------------------------------------------------------------
alter table llm_calls
    add column model_provider    text,
    add column model_version     text,
    -- Which row this generation explains. AI output is always an ATTACHMENT to
    -- evidence, never a row of evidence itself.
    add column attached_to_table text,
    add column attached_to_id    text,
    add column generated_at      timestamptz not null default now();

comment on column llm_calls.derived_from is
    'Evidence references the generation was grounded in — the rows whose values were placed in the prompt.';

-- A generation must say what produced it and what it was grounded in.
alter table llm_calls add constraint generations_are_accountable
    check (
        status <> 'ok'
        or purpose = 'strategist_chat'
        or (model_provider is not null and derived_from <> '{}'::jsonb)
    );

-- ---------------------------------------------------------------------------
-- The provenance index now says which contract each row is under, so "can this
-- number be reproduced?" is answered by the data rather than by knowing which
-- table you are looking at.
-- ---------------------------------------------------------------------------
create or replace view provenance_index as
select 'score_snapshots' as table_name, id::text as record_id, website_id,
       source, derived_from, observed_from, observed_to, computed_at,
       scoring_version as calculation_version, deterministic
from score_snapshots
union all
select 'issues', id::text, website_id, source, derived_from,
       observed_from, observed_to, computed_at, calculation_version, deterministic
from issues
union all
select 'recommendations', id::text, website_id, source, derived_from,
       observed_from, observed_to, computed_at, calculation_version, deterministic
from recommendations
union all
select 'external_metrics', id::text, website_id, source, derived_from,
       as_of, as_of, computed_at, calculation_version, deterministic
from external_metrics;
