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
    scope_type      text not null check (scope_type in ('website','page','keyword')),
    -- Contribution to the scorecard component named by `category`.
    score_weight    numeric not null default 1,
    effort          text not null default 'medium' check (effort in ('low','medium','high')),
    docs_url        text,
    -- Set false to retire a rule without deleting history.
    active          boolean not null default true
);

create table issues (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id)  on delete cascade,
    website_id         uuid not null references websites(id) on delete cascade,
    type_key        text not null references issue_types(key),
    scope_type      text not null check (scope_type in ('website','page','keyword')),
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
    dismissed_by    uuid references users(id) on delete set null,
    dismissed_reason text,
    unique (website_id, fingerprint)
);
create index on issues (website_id, status, severity);
create index on issues (website_id, impact_score desc) where status = 'open';
create index on issues (page_id);

-- Append-only. Every crawl writes one row per evaluated issue, present or not.
-- This is the table that answers "what changed" and proves a fix held.
create table issue_observations (
    id          bigserial primary key,
    issue_id    uuid not null references issues(id) on delete cascade,
    website_id     uuid not null,
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
    organization_id      uuid  not null references organizations(id)  on delete cascade,
    website_id     uuid  not null references websites(id) on delete cascade,
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
    unique (website_id, phrase_hash, country, device)
);
create index on keywords (website_id) where is_tracked;

-- ---------------------------------------------------------------------------
-- Scores
-- ---------------------------------------------------------------------------
-- scoring_version is not decoration. When weights change, every historical
-- snapshot is RECOMPUTED under the new version and the chart is redrawn from
-- one version throughout. A score that moves for reasons the user cannot trace
-- is worse than no score at all.
create table score_snapshots (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null,
    website_id         uuid not null references websites(id) on delete cascade,
    as_of           date not null,
    scoring_version text not null,
    -- Named per the V1 spec (s17). This is a Visibility Health Score computed
    -- by this platform; it is never presented as a Google score.
    total               numeric(5,2) not null,
    technical_health    numeric(5,2),
    search_performance  numeric(5,2),
    content_health      numeric(5,2),
    analytics_coverage  numeric(5,2),
    ai_visibility       numeric(5,2),
    -- Every input that produced the numbers above, so any point on the chart
    -- can be explained without re-deriving it.
    components      jsonb not null,
    crawl_id        uuid,
    computed_at     timestamptz not null default now(),
    unique (website_id, as_of, scoring_version)
);

-- ---------------------------------------------------------------------------
-- Weekly plan and recommendations
-- ---------------------------------------------------------------------------
create table plans (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null,
    website_id         uuid not null references websites(id) on delete cascade,
    week_start      date not null,
    summary_md      text,
    scoring_version text not null,
    prompt_version  text not null,
    model           text not null,
    generated_at    timestamptz not null default now(),
    unique (website_id, week_start)
);

create table recommendations (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null,
    website_id         uuid not null references websites(id) on delete cascade,
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
    -- Statuses fixed by the V1 spec (s25).
    status          text not null default 'OPEN'
                         check (status in ('OPEN','IN_PROGRESS','RESOLVED','DISMISSED')),
    resolved_at     timestamptz,
    created_at      timestamptz not null default now()
);
create index on recommendations (website_id, plan_id, rank);

-- The fix/verify half of the loop, and the substrate for every future
-- capability that WRITES to a system the customer owns: Business Profile
-- posts, WordPress edits, Ads changes, automated publishing. Those differ only
-- in `capability` and `provider_key`; the approval gate, the before-state, the
-- idempotency key and the revert path are identical and are designed once,
-- here, rather than retrofitted under four features at once.
--
-- Invariant: no row may reach status 'applied' without a before_state. A
-- change that cannot be reverted is not applied.
create table actions (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null,
    website_id         uuid not null references websites(id) on delete cascade,
    issue_id        uuid references issues(id) on delete set null,
    recommendation_id uuid references recommendations(id) on delete set null,

    -- Dotted capability, checked against action_capabilities (0007):
    --   page.meta.update | wp.post.update | gbp.post.create | ads.budget.update
    capability      text not null,
    provider_key    text not null default 'manual',
    -- Credentials used for an external write; null when the user did it by hand.
    connection_id   uuid references connections(id) on delete set null,

    target_kind     text not null default 'page'
                         check (target_kind in ('page','website','location','campaign','post')),
    target_ref      text,

    -- External writes must be exactly-once across retries.
    idempotency_key text unique,

    -- Every write to a customer-owned system passes an approval gate. The
    -- default is the most conservative; standing approval is opt-in per
    -- capability and revocable.
    approval_mode   text not null default 'explicit_approval'
                         check (approval_mode in ('manual_only','explicit_approval','standing_approval')),
    approved_by     uuid references users(id) on delete set null,
    approved_at     timestamptz,

    before_state    jsonb,
    after_state     jsonb,
    diff            jsonb,

    status          text not null default 'draft'
                         check (status in ('draft','awaiting_approval','approved','applying',
                                           'applied','failed','reverted')),
    applied_by      uuid references users(id) on delete set null,
    applied_at      timestamptz,
    reverted_at     timestamptz,
    -- Points at the action this one undoes, so a revert is itself auditable.
    reverts_action_id uuid references actions(id) on delete set null,

    verification_crawl_id uuid references crawls(id) on delete set null,
    verified_at     timestamptz,
    result          text not null default 'pending'
                         check (result in ('pending','succeeded','failed','reverted','unverified')),
    error           text,
    notes           text,
    created_at      timestamptz not null default now(),

    constraint applied_actions_are_revertible
        check (status not in ('applied','reverted') or before_state is not null)
);
create index on actions (website_id, applied_at desc);
create index on actions (status) where status in ('awaiting_approval','applying');

-- ---------------------------------------------------------------------------
-- Reporting and AI metering
-- ---------------------------------------------------------------------------
create table reports (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null,
    website_id         uuid not null references websites(id) on delete cascade,
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
    unique (website_id, kind, period_start)
);

-- Every model call is metered. This is what enforces per-org budgets and what
-- tells you, per feature, whether the unit economics work.
create table llm_calls (
    id              bigserial primary key,
    organization_id          uuid,
    website_id         uuid,
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
create index on llm_calls (organization_id, created_at desc);
