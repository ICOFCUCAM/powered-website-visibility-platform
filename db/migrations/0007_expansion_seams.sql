-- 0007_expansion_seams.sql
--
-- Seams for the phase 2-4 roadmap. Nothing here is used by the MVP. It exists
-- because each of these is cheap to add to an empty database and expensive to
-- retrofit once there is data and code depending on the old shape.
--
-- The test applied to every table below: "if this is missing, does adding the
-- feature later require rewriting something that already works?" If no, it was
-- left out. Empty tables are not free — see the UI gating rule at the bottom.

-- ---------------------------------------------------------------------------
-- 1. VISIBILITY SURFACES
-- The MVP measures one surface: organic search. The roadmap adds AI answers,
-- Maps (Business Profile), social and paid. Making the surface a first-class
-- row means a new one is a seed row plus a collector, not a schema change and
-- a scorecard rewrite.
-- ---------------------------------------------------------------------------
create table visibility_surfaces (
    key         text primary key,
    label       text not null,
    phase       int  not null default 1,
    enabled     boolean not null default false
);
insert into visibility_surfaces (key, label, phase, enabled) values
    ('organic_search','Google organic search', 1, true),
    ('ai_search',     'AI answer engines',     3, false),
    ('maps',          'Google Maps & local',   3, false),
    ('paid',          'Paid search',           3, false),
    ('social',        'Social platforms',      4, false);

create table website_surfaces (
    organization_id      uuid not null references organizations(id)  on delete cascade,
    website_id     uuid not null references websites(id) on delete cascade,
    surface_key text not null references visibility_surfaces(key),
    enabled     boolean not null default true,
    config      jsonb   not null default '{}'::jsonb,
    primary key (website_id, surface_key)
);

-- ---------------------------------------------------------------------------
-- 2. SCORECARD AS CONFIGURATION
-- Adding "Maps visibility" or "Social visibility" to the scorecard must be a
-- config row and a recompute, not an edit to scoring.py and a silent shift in
-- everyone's number. `is_modelled` is what lets the UI label a component
-- honestly when its inputs are proxies rather than measurements.
-- ---------------------------------------------------------------------------
create table score_components (
    scoring_version text not null,
    key             text not null,
    label           text not null,
    surface_key     text references visibility_surfaces(key),
    weight          numeric not null,
    data_source     text not null,
    is_modelled     boolean not null default false,
    -- A component with no data source is ABSENT from the score, never zero.
    enabled         boolean not null default true,
    phase           int not null default 1,
    primary key (scoring_version, key)
);
insert into score_components
    (scoring_version, key, label, surface_key, weight, data_source, is_modelled, enabled, phase) values
    ('1.0.0','technical_health',   'Technical Health',   'organic_search', 0.30, 'crawl+crux',     false, true,  1),
    ('1.0.0','search_performance', 'Search Performance', 'organic_search', 0.35, 'search_console', false, true,  1),
    ('1.0.0','content_health',     'Content Health',     'organic_search', 0.25, 'crawl',          false, true,  1),
    ('1.0.0','analytics_coverage', 'Analytics Coverage', 'organic_search', 0.10, 'ga4+crawl',      false, true,  1),
    ('1.0.0','ai_visibility',      'AI visibility',      'ai_search',      0.00, 'crawl_proxies',   true, false, 3),
    ('1.0.0','authority',          'Authority',          'organic_search', 0.00, 'backlink_vendor', false,false, 2);

-- ---------------------------------------------------------------------------
-- 3. WRITE CAPABILITIES
-- Catalogue backing `actions.capability`. Business Profile management,
-- WordPress auto-editing, automated publishing and Ads management all land
-- here as rows. `blast_radius` is what decides which tier may ever run
-- unattended: only `reversible` capabilities are eligible for standing
-- approval, and `destructive` ones never are.
-- ---------------------------------------------------------------------------
create table action_capabilities (
    key             text primary key,
    label           text not null,
    provider_key    text not null references providers(key),
    service         text,
    blast_radius    text not null check (blast_radius in ('reversible','significant','destructive')),
    default_approval text not null default 'explicit_approval'
                          check (default_approval in ('manual_only','explicit_approval','standing_approval')),
    allows_standing_approval boolean not null default false,
    phase           int not null default 3,
    enabled         boolean not null default false
);
insert into action_capabilities
    (key, label, provider_key, service, blast_radius, default_approval, allows_standing_approval, phase, enabled) values
    ('manual.mark_fixed',   'User marked an issue fixed', 'manual', null, 'reversible','manual_only', false, 1, true),
    ('page.meta.update',    'Update title or description','wordpress','website','reversible','explicit_approval', true, 3, false),
    ('page.image.compress', 'Compress an image',          'wordpress','website','reversible','explicit_approval', true, 3, false),
    ('wp.post.update',      'Edit a post or page',        'wordpress','website','significant','explicit_approval', false, 3, false),
    ('wp.post.publish',     'Publish a draft',            'wordpress','website','significant','explicit_approval', false, 3, false),
    ('gbp.post.create',     'Post to Business Profile',   'google','business_profile','significant','explicit_approval', false, 3, false),
    ('gbp.info.update',     'Update business information','google','business_profile','significant','explicit_approval', false, 3, false),
    ('gbp.review.reply',    'Reply to a review',          'google','business_profile','significant','explicit_approval', false, 3, false),
    ('ads.budget.update',   'Change a campaign budget',   'google','ads','destructive','explicit_approval', false, 3, false),
    ('website.redirect.create','Create a redirect',          'wordpress','website','destructive','explicit_approval', false, 4, false);

-- ---------------------------------------------------------------------------
-- 4. THIRD-PARTY DATA
-- THE APPLICATION OWNS THE DATA MODEL; THE VENDOR OWNS ACQUISITION.
-- Backlinks, keyword volume, competitor traffic and industry benchmarks all
-- arrive from vendors that may be swapped. The schema is therefore written
-- against the domain, not against any vendor's response shape, and every value
-- carries its provenance so a modelled figure can never be rendered as a
-- measurement.
-- ---------------------------------------------------------------------------
create table data_providers (
    key             text primary key,
    label           text not null,
    capabilities    text[] not null default '{}',   -- backlinks|keyword_volume|serp|traffic_estimate|benchmarks
    status          text not null default 'evaluating'
                         check (status in ('evaluating','active','suspended','retired')),
    -- Populated from the commercial evaluation; drives per-plan admission.
    unit_cost_usd   numeric(10,6),
    notes           text
);

create table referring_domains (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id)  on delete cascade,
    website_id         uuid not null references websites(id) on delete cascade,
    domain          citext not null,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),
    lost_at         timestamptz,
    links_count     int not null default 0,
    -- Vendor-scaled metric; meaningless across vendors, so the source is
    -- recorded with it and the UI never compares two sources' figures.
    authority_score numeric(6,2),
    authority_source text references data_providers(key),
    provider_key    text not null references data_providers(key),
    unique (website_id, domain)
);
create index on referring_domains (website_id) where lost_at is null;

create table backlinks (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id)  on delete cascade,
    website_id         uuid not null references websites(id) on delete cascade,
    referring_domain_id uuid references referring_domains(id) on delete cascade,
    source_url      text  not null,
    source_url_hash bytea not null,
    target_url      text  not null,
    anchor_text     text,
    rel             text[],
    is_nofollow     boolean not null default false,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),
    lost_at         timestamptz,
    provider_key    text not null references data_providers(key),
    unique (website_id, source_url_hash, target_url)
);
create index on backlinks (website_id, first_seen_at desc);

-- Append-only new/lost history, so "+17 new, -4 lost" is a query over facts
-- rather than a diff recomputed on the fly.
create table backlink_changes (
    id              bigserial primary key,
    organization_id          uuid not null,
    website_id         uuid not null,
    observed_on     date not null,
    change          text not null check (change in ('new','lost')),
    backlink_id     uuid,
    referring_domain citext,
    provider_key    text not null
);
create index on backlink_changes (website_id, observed_on desc);

-- Any vendor-supplied metric that is not a first-class table: keyword volume,
-- competitor traffic estimates, industry benchmarks. `is_modelled` is not
-- decoration — the UI is required to label or suppress modelled values, and a
-- modelled number is never presented as fact.
create table external_metrics (
    id              bigserial primary key,
    organization_id          uuid not null references organizations(id) on delete cascade,
    website_id         uuid references websites(id) on delete cascade,
    subject_kind    text not null check (subject_kind in ('website','domain','keyword','page','industry')),
    subject_ref     text not null,
    metric          text not null,
    value_numeric   numeric,
    value_text      text,
    unit            text,
    provider_key    text not null references data_providers(key),
    is_modelled     boolean not null default true,
    confidence      numeric(4,3),
    as_of           date not null,
    fetched_at      timestamptz not null default now(),
    unique (subject_kind, subject_ref, metric, provider_key, as_of)
);
create index on external_metrics (website_id, metric, as_of desc);

-- ---------------------------------------------------------------------------
-- 5. COMPETITORS (phase 2)
-- A competitor is a domain the org tracks. Its own-website crawl data comes from
-- the same crawler under a different corpus (see 6), and its keyword/backlink
-- figures come from external_metrics. Nothing here needs a vendor chosen.
-- ---------------------------------------------------------------------------
create table competitors (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id)  on delete cascade,
    website_id         uuid not null references websites(id) on delete cascade,
    domain          citext not null,
    label           text,
    added_by        uuid references users(id) on delete set null,
    is_active       boolean not null default true,
    created_at      timestamptz not null default now(),
    unique (website_id, domain)
);

-- ---------------------------------------------------------------------------
-- 6. CRAWL CORPUS AND EXTRACTION REPLAY
-- The MVP crawls tenant websites. Phase 2 crawls competitors; phase 4 contemplates
-- a web-scale index. Tagging the corpus keeps non-tenant pages out of tenant
-- analytics while reusing one pipeline, and storing a VERSIONED EXTRACTION
-- DOCUMENT alongside the raw HTML means a future index can be built by
-- replaying extraction over a different corpus without touching Postgres.
-- ---------------------------------------------------------------------------
alter table crawls add column corpus text not null default 'tenant_site'
    check (corpus in ('tenant_site','competitor','index'));
alter table crawls add column competitor_id uuid references competitors(id) on delete cascade;
alter table page_snapshots add column extract_version text not null default 'v1';
alter table page_snapshots add column extract_key text;

-- ---------------------------------------------------------------------------
-- 7. AGENCY AND WHITE-LABEL (phase 4)
-- Org hierarchy is the one item here that is genuinely painful to retrofit:
-- every permission check would need revisiting. A nullable column now costs
-- nothing.
-- ---------------------------------------------------------------------------
alter table organizations add column parent_organization_id uuid references organizations(id) on delete set null;
alter table organizations add column region text not null default 'eu-west-1';
create index on organizations (parent_organization_id) where parent_organization_id is not null;

create table organization_branding (
    organization_id          uuid primary key references organizations(id) on delete cascade,
    logo_key        text,
    primary_color   text,
    custom_domain   citext unique,
    email_from_name text,
    report_footer   text,
    white_label     boolean not null default false,
    updated_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 8. PUBLIC API AND AUDIT (phase 4 / enterprise)
-- The audit log is the other retrofit that hurts: it cannot be backfilled, and
-- the first enterprise customer will ask for history that does not exist.
-- ---------------------------------------------------------------------------
create table api_keys (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id) on delete cascade,
    name            text not null,
    prefix          text not null unique,
    key_hash        bytea not null,
    scopes          text[] not null default '{}',
    created_by      uuid references users(id) on delete set null,
    created_at      timestamptz not null default now(),
    last_used_at    timestamptz,
    revoked_at      timestamptz
);

create table audit_log (
    id              bigserial primary key,
    organization_id          uuid not null,
    actor_kind      text not null check (actor_kind in ('user','api_key','system','ai')),
    actor_user_id   uuid,
    actor_api_key_id uuid,
    action          text not null,
    subject_kind    text,
    subject_ref     text,
    metadata        jsonb not null default '{}'::jsonb,
    ip              inet,
    created_at      timestamptz not null default now()
);
create index on audit_log (organization_id, created_at desc);

-- ---------------------------------------------------------------------------
-- 9. MONITORING AND ALERTS (phase 3)
-- ---------------------------------------------------------------------------
create table alert_rules (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id)  on delete cascade,
    website_id         uuid not null references websites(id) on delete cascade,
    kind            text not null,     -- traffic_drop | ranking_drop | site_down | new_critical_issue
    threshold       jsonb not null default '{}'::jsonb,
    channels        text[] not null default '{email}',
    is_active       boolean not null default true,
    created_at      timestamptz not null default now()
);

create table alert_events (
    id              bigserial primary key,
    organization_id          uuid not null,
    website_id         uuid not null,
    rule_id         uuid references alert_rules(id) on delete set null,
    severity        text not null,
    title           text not null,
    evidence        jsonb not null default '{}'::jsonb,
    fired_at        timestamptz not null default now(),
    delivered_at    timestamptz,
    acknowledged_at timestamptz
);
create index on alert_events (website_id, fired_at desc);

-- ---------------------------------------------------------------------------
-- 10. CONTENT (phase 3)
-- The review state is structural, not a policy note: there is no column and no
-- status that lets generated content reach a live website without a human
-- approving an `actions` row.
-- ---------------------------------------------------------------------------
create table content_drafts (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id)  on delete cascade,
    website_id         uuid not null references websites(id) on delete cascade,
    kind            text not null check (kind in ('article','brief','faq','meta','social')),
    title           text,
    body_md         text,
    target_keywords text[],
    source_recommendation_id uuid references recommendations(id) on delete set null,
    model           text,
    prompt_version  text,
    status          text not null default 'draft'
                         check (status in ('draft','in_review','approved','published','discarded')),
    reviewed_by     uuid references users(id) on delete set null,
    reviewed_at     timestamptz,
    -- Publication happens only through an approved action row.
    published_action_id uuid references actions(id) on delete set null,
    created_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- RLS for everything added above.
-- ---------------------------------------------------------------------------
do $$
declare t text;
begin
    foreach t in array array[
        'website_surfaces','referring_domains','backlinks','external_metrics',
        'competitors','organization_branding','api_keys','alert_rules','content_drafts'
    ] loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format(
            'create policy %I on %I for select using (app.is_org_member(organization_id))',
            t || '_read', t);
        execute format(
            'create policy %I on %I for all using (app.can_write_org(organization_id))
                                        with check (app.can_write_org(organization_id))',
            t || '_write', t);
    end loop;
end $$;

-- organization_branding keys on organization_id rather than carrying one, so it needs its own.
drop policy if exists org_branding_read  on organization_branding;
drop policy if exists org_branding_write on organization_branding;
create policy org_branding_read on organization_branding for select
    using (app.is_org_member(organization_id));
create policy org_branding_write on organization_branding for all
    using (app.can_write_org(organization_id)) with check (app.can_write_org(organization_id));

alter table backlink_changes enable row level security;
alter table alert_events     enable row level security;
alter table audit_log        enable row level security;

-- Catalogues are readable by everyone; only migrations write them.
do $$
declare t text;
begin
    foreach t in array array['visibility_surfaces','score_components',
                             'action_capabilities','data_providers'] loop
        execute format('alter table %I enable row level security', t);
        execute format('create policy %I on %I for select using (true)', t || '_read', t);
    end loop;
end $$;

-- ---------------------------------------------------------------------------
-- THE GATING RULE THAT MAKES EMPTY TABLES SAFE
--
-- A table existing must never cause the UI to render a zero. Every surface,
-- score component and provider-backed panel is gated on its catalogue row
-- being `enabled` AND on data being present. Until a backlink vendor is
-- contracted, `score_components` has authority at weight 0 and enabled=false,
-- so the scorecard omits it rather than showing "Authority 0".
-- ---------------------------------------------------------------------------
