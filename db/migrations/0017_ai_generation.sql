-- 0017_ai_generation.sql
--
-- The AI layer's storage. Three ideas, and they are all about the same thing:
-- keeping generated prose clearly separable from measured fact.
--
--   1. An explanation is CACHED ON ITS EVIDENCE, not on the issue. The same
--      missing-title problem, with byte-identical evidence, is one generation
--      shared by every website that has it. On a 500-page crawl this is the
--      difference between ~200 calls and ~5.
--
--   2. Every artefact says WHOSE WORDS THEY ARE. A plan written by a model and
--      a plan rendered from a template are both legitimate outputs; a customer
--      reading one while believing it is the other is not.
--
--   3. Nothing generated is ever evidence. The ranking, the impact score and
--      the selection stay in code (see `recommendations.deterministic`); the
--      model supplies prose for rows that already exist.

-- ---------------------------------------------------------------------------
-- The explanation cache
-- ---------------------------------------------------------------------------
-- DELIBERATELY NOT A TENANT TABLE, and that needs justifying rather than
-- assuming, because every other table here carries organization_id.
--
-- `cache_key` is a hash of the COMPLETE prompt payload. Two organisations can
-- therefore only share a row when the text they would each have been shown is
-- identical — anything tenant-identifying (a URL, a domain, a query) changes
-- the payload and so changes the key. Sharing a row leaks nothing that both
-- parties would not have received anyway, and `api/ai/cache.py` derives the
-- key from the payload it actually sends, so a field cannot enter the prompt
-- without entering the key.
create table issue_explanations (
    cache_key       text primary key,
    prompt_version  text not null,
    type_key        text not null references issue_types(key),

    -- What the customer reads.
    what            text not null,
    why             text not null,
    how             text[] not null default '{}',
    effort          text not null check (effort in ('low','medium','high')),

    -- Accountability, not reproducibility (see 0011). A generation records
    -- what produced it and what it was grounded in; it makes no claim that
    -- the same inputs would produce the same words again.
    model_provider  text not null,
    model           text not null,
    evidence        jsonb not null,
    deterministic   boolean not null default false,
    generated_at    timestamptz not null default now(),
    -- Cheap telemetry for "is the cache actually working".
    hit_count       bigint not null default 0,
    last_used_at    timestamptz not null default now()
);

comment on table issue_explanations is
    'Global generation cache keyed on the complete prompt payload. Not tenant-scoped: an identical key means identical input, so a shared row reveals nothing a tenant would not already have been shown.';

create index on issue_explanations (type_key, prompt_version);

-- ---------------------------------------------------------------------------
-- Plans
-- ---------------------------------------------------------------------------
-- `model` was already NOT NULL, which is right: a plan always names its
-- author, and 'template' is an author.
alter table plans
    add column model_provider text not null default 'template',
    -- A plan is never reproducible word-for-word. It is here as an explicit
    -- false rather than absent so the provenance index can answer the question
    -- for plans the same way it answers it for scores.
    add column deterministic boolean not null default false,
    -- Set when the model was not used, or was used and rejected. Null means
    -- the generation was accepted.
    add column fallback_reason text
        check (fallback_reason is null or fallback_reason in
               ('no_provider','budget_exhausted','validation_failed',
                'provider_error','unsupported_numbers','reordered_priorities'));

comment on column plans.fallback_reason is
    'Why this plan is templated rather than generated. Null means a model wrote it and the output passed validation.';

-- ---------------------------------------------------------------------------
-- Recommendations
-- ---------------------------------------------------------------------------
-- The ranking above a recommendation and the prose inside it have different
-- authors, and the row should say so rather than leaving a reader to guess
-- which parts a model touched.
alter table recommendations
    add column prose_source text not null default 'template'
        check (prose_source in ('template','model'));

comment on column recommendations.prose_source is
    'Who wrote title/body_md/how_to_md. rank, impact_score and the selection are always code — see the deterministic column.';

-- ---------------------------------------------------------------------------
-- Reports
-- ---------------------------------------------------------------------------
alter table reports
    add column subject text,
    -- The rendered figures, kept beside the HTML so a disputed number in a
    -- sent email can be traced without re-deriving it from tables that have
    -- since moved on.
    add column payload jsonb not null default '{}'::jsonb;

-- ---------------------------------------------------------------------------
-- Metering
-- ---------------------------------------------------------------------------
-- Budget admission sums this month's spend per organisation on every
-- generation attempt, so it needs to be an index-only scan rather than a walk
-- of the org's whole call history.
create index llm_calls_org_month on llm_calls (organization_id, created_at)
    include (cost_usd);

-- A cost we could not price is NULL, never 0. Recording an unpriced call as
-- free would make a budget quietly unenforceable, which is the one failure
-- mode of a spend limit that nobody notices until the invoice.
comment on column llm_calls.cost_usd is
    'Null when the model has no entry in api/ai/pricing.py. Never defaulted to zero: an unpriced call must be visible, not free.';
