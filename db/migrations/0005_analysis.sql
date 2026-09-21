-- 0005_analysis.sql
-- Issues, keywords, scores, recommendations, and the fix/verify loop.
--
-- The central idea: an ISSUE IS AN ENTITY, NOT A MESSAGE. It has a stable
-- fingerprint, a lifecycle, and an append-only observation history. If issues
-- were regenerated from the model each crawl, the user would get a fresh list
-- of near-duplicate advice every week, "what changed" would be impossible, and
-- nothing could ever be proved fixed.

-- Static catalogue, seeded from code so rules and rows cannot drift.
create table issue_types (
    key             text primary key,
    category        text not null
                         check (category in ('technical','content','seo','authority','ai_search')),
    default_severity text not null
                         check (default_severity in ('critical','high','medium','low','info')),
    title           text not null,
    summary         text not null,
    scope_type      text not null check (scope_type in ('site','page','keyword')),
    -- Contribution to the scorecard component named by `category`.
    score_weight    numeric not null default 1,
    effort          text not null default 'medium' check (effort in ('low','medium','high')),
    docs_url        text,
    -- Set false to retire a rule without deleting history.
    active          boolean not null default true
);

create table issues (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null references orgs(id)  on delete cascade,
    site_id         uuid not null references sites(id) on delete cascade,
    type_key        text not null references issue_types(key),
    scope_type      text not null check (scope_type in ('site','page','keyword')),
    page_id         uuid references pages(id)    on delete cascade,
    keyword_id      uuid,
    -- sha256("{type_key}:{scope_type}:{scope_ref}") — deterministic, so the
    -- same problem on the same URL is the same row in week 1 and week 40.
    fingerprint     text not null,
    severity        text not null,
    status          text not null default 'open'
                         check (status in ('open','snoozed','dismissed','applied',
                                           'verified','regressed','resolved')),
    -- Populated from the evidence, e.g. estimated monthly clicks recoverable.
    impact_score    numeric not null default 0,
    evidence        jsonb   not null default '{}'::jsonb,
    first_detected_at timestamptz not null default now(),
    last_detected_at  timestamptz not null default now(),
    resolved_at     timestamptz,
    snoozed_until   timestamptz,
    dismissed_by    uuid references profiles(id) on delete set null,
    dismissed_reason text,
    unique (site_id, fingerprint)
);
create index on issues (site_id, status, severity);
create index on issues (site_id, impact_score desc) where status = 'open';
create index on issues (page_id);

-- Append-only. Every crawl writes one row per evaluated issue, present or not.
-- This is the table that answers "what changed" and proves a fix held.
create table issue_observations (
    id          bigserial primary key,
    issue_id    uuid not null references issues(id) on delete cascade,
    site_id     uuid not null,
    crawl_id    uuid,
    observed_at timestamptz not null default now(),
    present     boolean not null,
    severity    text,
    evidence    jsonb
);
create index on issue_observations (issue_id, observed_at desc);

-- ---------------------------------------------------------------------------
-- Keywords
-- ---------------------------------------------------------------------------
-- Metrics are NOT stored here. They live in gsc_query_daily and are joined on
-- phrase_hash. A keyword row is a tracking decision, not a data point. Volume
-- and difficulty columns are deliberately absent until a real data source is
-- licensed: a model-generated search volume is a fabricated number.
create table keywords (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid  not null references orgs(id)  on delete cascade,
    site_id     uuid  not null references sites(id) on delete cascade,
    phrase      citext not null,
    phrase_hash bytea not null,
    source      text  not null
                      check (source in ('gsc','ai_suggested','manual','competitor')),
    is_tracked  boolean not null default false,
    country     text default 'ZZZ',
    device      text default 'ALL',
    intent      text check (intent in ('informational','commercial','navigational','local')),
    topic       text,
    created_at  timestamptz not null default now(),
    unique (site_id, phrase_hash, country, device)
);
create index on keywords (site_id) where is_tracked;

-- ---------------------------------------------------------------------------
-- Scores
-- ---------------------------------------------------------------------------
-- scoring_version is not decoration. When weights change, every historical
-- snapshot is RECOMPUTED under the new version and the chart is redrawn from
-- one version throughout. A score that moves for reasons the user cannot trace
-- is worse than no score at all.
create table score_snapshots (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null,
    site_id         uuid not null references sites(id) on delete cascade,
    as_of           date not null,
    scoring_version text not null,
    total           numeric(5,2) not null,
    technical_seo   numeric(5,2),
    google_visibility numeric(5,2),
    content         numeric(5,2),
    authority       numeric(5,2),
    ai_visibility   numeric(5,2),
    -- Every input that produced the numbers above, so any point on the chart
    -- can be explained without re-deriving it.
    components      jsonb not null,
    crawl_id        uuid,
    computed_at     timestamptz not null default now(),
    unique (site_id, as_of, scoring_version)
);

-- ---------------------------------------------------------------------------
-- Weekly plan and recommendations
-- ---------------------------------------------------------------------------
create table plans (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null,
    site_id         uuid not null references sites(id) on delete cascade,
    week_start      date not null,
    summary_md      text,
    scoring_version text not null,
    prompt_version  text not null,
    model           text not null,
    generated_at    timestamptz not null default now(),
    unique (site_id, week_start)
);

create table recommendations (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null,
    site_id         uuid not null references sites(id) on delete cascade,
    plan_id         uuid references plans(id)  on delete cascade,
    issue_id        uuid references issues(id) on delete set null,
    kind            text not null
                         check (kind in ('fix_issue','improve_ctr','create_content',
                                         'internal_links','technical','other')),
    rank            int  not null,
    title           text not null,
    body_md         text,
    how_to_md       text,
    -- Ranking inputs, computed in code. The model writes prose, not priority.
    impact_score    numeric not null,
    estimated_clicks_delta numeric,
    effort          text not null check (effort in ('low','medium','high')),
    confidence      numeric(4,3) not null,
    targets         jsonb not null default '{}'::jsonb,   -- page_ids, keywords
    status          text not null default 'proposed'
                         check (status in ('proposed','accepted','done','dismissed','expired')),
    created_at      timestamptz not null default now()
);
create index on recommendations (site_id, plan_id, rank);

-- The fix/verify half of the loop. Ships in v3 for applied changes, but the
-- table exists from v1 so a manually-confirmed fix can still be verified.
create table fix_actions (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null,
    site_id         uuid not null references sites(id) on delete cascade,
    issue_id        uuid references issues(id) on delete set null,
    recommendation_id uuid references recommendations(id) on delete set null,
    action_type     text not null,
    target_url      text,
    channel         text not null default 'manual'
                         check (channel in ('manual','wordpress','api')),
    -- Before-state is what makes revert possible. Never write an action row
    -- without it.
    before_state    jsonb,
    after_state     jsonb,
    applied_by      uuid references profiles(id) on delete set null,
    applied_at      timestamptz,
    reverted_at     timestamptz,
    verification_crawl_id uuid references crawls(id) on delete set null,
    verified_at     timestamptz,
    result          text not null default 'pending'
                         check (result in ('pending','succeeded','failed','reverted','unverified')),
    notes           text
);
create index on fix_actions (site_id, applied_at desc);

-- ---------------------------------------------------------------------------
-- Reporting and AI metering
-- ---------------------------------------------------------------------------
create table reports (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null,
    site_id         uuid not null references sites(id) on delete cascade,
    kind            text not null default 'weekly' check (kind in ('weekly','monthly','adhoc')),
    period_start    date not null,
    period_end      date not null,
    plan_id         uuid references plans(id) on delete set null,
    html_key        text,
    pdf_key         text,
    status          text not null default 'draft'
                         check (status in ('draft','sent','failed')),
    recipients      text[],
    generated_at    timestamptz not null default now(),
    sent_at         timestamptz,
    error           text,
    unique (site_id, kind, period_start)
);

-- Every model call is metered. This is what enforces per-org budgets and what
-- tells you, per feature, whether the unit economics work.
create table llm_calls (
    id              bigserial primary key,
    org_id          uuid,
    site_id         uuid,
    purpose         text not null,
    model           text not null,
    prompt_version  text,
    input_tokens    int,
    output_tokens   int,
    cached_input_tokens int,
    cost_usd        numeric(10,6),
    cache_hit       boolean not null default false,
    latency_ms      int,
    status          text not null default 'ok' check (status in ('ok','error','refused')),
    created_at      timestamptz not null default now()
);
create index on llm_calls (org_id, created_at desc);
