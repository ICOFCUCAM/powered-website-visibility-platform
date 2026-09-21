-- 0002_connections.sql
-- The connection layer behind the Google Hub.
--
-- DELIBERATELY PROVIDER-AGNOSTIC. The MVP connects Search Console and
-- Analytics, but the roadmap adds Business Profile, Ads, WordPress, and social
-- analytics — all of which are "an account the user authorises, exposing
-- resources, some of which are attached to a website". Naming these tables
-- google_* would make every later provider either a lie or a migration.
--
--   connections        one authorised account at one provider
--   connection_properties  what that account can see (property, location, ...)
--   website_connections   which resource feeds which website, per capability
--
-- Adding a provider is then: a row in `providers`, a client module, a
-- discovery function. No schema change.

-- ---------------------------------------------------------------------------
-- Token vault. A separate schema because refresh tokens are the crown jewels
-- and must never be reachable from a tenant query, a view, an ORM relation or
-- a PostgREST expansion. No RLS policies and no grants exist here, so no
-- client role can read it under any circumstance. Backend service role only.
-- ---------------------------------------------------------------------------
create schema if not exists secrets;
revoke all on schema secrets from public;

create table secrets.oauth_tokens (
    id              uuid primary key default gen_random_uuid(),
    -- Envelope encryption: ciphertext here, data key wrapped by a KMS key.
    -- Plaintext exists only inside the process making the provider call.
    ciphertext      bytea       not null,
    wrapped_dek     bytea       not null,
    key_version     int         not null default 1,
    algo            text        not null default 'AES-256-GCM',
    nonce           bytea       not null,
    created_at      timestamptz not null default now(),
    rotated_at      timestamptz
);

-- ---------------------------------------------------------------------------
-- Provider and capability catalogue. Seeded from code. `phase` documents when
-- each arrives; the UI renders anything beyond the current phase as
-- "coming soon" rather than as a button that fails.
-- ---------------------------------------------------------------------------
create table providers (
    key         text primary key,          -- google | microsoft | wordpress | meta | ...
    label       text not null,
    auth_kind   text not null check (auth_kind in ('none','oauth2','api_key','app_password')),
    phase       int  not null default 1,
    enabled     boolean not null default false
);

create table provider_services (
    provider_key text not null references providers(key) on delete cascade,
    service      text not null,            -- search_console | analytics | business_profile | ads | ...
    label        text not null,
    -- Whether attaching this service to a website grants write capability. Read
    -- services can be connected freely; write services additionally require an
    -- approval mode on every action (see 0007).
    writes       boolean not null default false,
    scopes       text[]  not null default '{}',
    phase        int     not null default 1,
    enabled      boolean not null default false,
    primary key (provider_key, service)
);

insert into providers (key, label, auth_kind, phase, enabled) values
    -- 'manual' is the provider for a change the user made themselves. It keeps
    -- the action ledger uniform: a hand-edited title and a WordPress write
    -- differ only in provider and capability.
    ('manual',    'Manual',    'none',         1, true),
    ('google',    'Google',    'oauth2',       1, true),
    ('wordpress', 'WordPress', 'app_password', 3, false),
    ('microsoft', 'Microsoft', 'oauth2',       4, false),
    ('meta',      'Meta',      'oauth2',       4, false);

insert into provider_services (provider_key, service, label, writes, scopes, phase, enabled) values
    ('google','search_console','Google Search Console', false,
     '{https://www.googleapis.com/auth/webmasters.readonly}', 1, true),
    ('google','analytics','Google Analytics', false,
     '{https://www.googleapis.com/auth/analytics.readonly}', 1, true),
    ('google','business_profile','Google Business Profile', true,
     '{https://www.googleapis.com/auth/business.manage}', 3, false),
    ('google','ads','Google Ads', true,
     '{https://www.googleapis.com/auth/adwords}', 3, false),
    ('wordpress','website','WordPress website', true, '{}', 3, false),
    ('microsoft','bing_webmaster','Bing Webmaster Tools', false, '{}', 4, false),
    ('meta','pages','Facebook & Instagram', false, '{}', 4, false);

-- ---------------------------------------------------------------------------
-- An authorised account at a provider.
-- ---------------------------------------------------------------------------
create table connections (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id) on delete cascade,
    provider_key    text not null references providers(key),
    -- Provider's stable subject id. For Google this is the OIDC `sub` claim,
    -- which the V1 spec calls google_subject_id. NEVER key on email: users
    -- change it and one account would silently fork into two.
    external_id     text   not null,
    label           citext not null,           -- email, website URL, page name
    avatar_url      text,
    -- What was actually granted, which can be narrower than what was asked
    -- for. Features gate on this, never on the requested set.
    granted_scopes  text[] not null default '{}',
    refresh_token_id uuid references secrets.oauth_tokens(id) on delete set null,
    -- Access tokens are short-lived and held in a cache, not persisted. Only
    -- the expiry is stored, so a worker knows whether to refresh before use.
    access_token_expires_at timestamptz,
    status          text not null default 'active'
                         check (status in ('active','needs_reauth','revoked','error')),
    last_error      text,
    last_refreshed_at timestamptz,
    connected_by    uuid references users(id) on delete set null,
    created_at      timestamptz not null default now(),
    revoked_at      timestamptz,
    unique (organization_id, provider_key, external_id)
);
create index on connections (organization_id) where status = 'active';

-- ---------------------------------------------------------------------------
-- Everything discovered after consent, across every service, in one table.
-- This is what the wizard's "Choose your website" step renders.
-- ---------------------------------------------------------------------------
create table connection_properties (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id) on delete cascade,
    connection_id   uuid not null references connections(id) on delete cascade,
    provider_key    text not null references providers(key),
    service         text not null,
    -- Service-native identifier, stored verbatim:
    --   search_console    sc-domain:example.com | https://www.example.com/
    --   analytics         properties/123456789
    --   business_profile  locations/0123456789
    --   ads               customers/1234567890
    property_uri    text not null,
    property_name   text,
    permission_level text,
    -- Normalised host(s) this resource concerns, used to auto-match it to a
    -- website. A GSC domain property expands to the bare domain; a URL-prefix
    -- property to its host; a GBP location to its website field.
    matched_hosts   text[] not null default '{}',
    -- Physical location, for Business Profile and local surfaces (phase 3).
    geo             jsonb,
    raw             jsonb not null default '{}'::jsonb,
    discovered_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),
    unique (connection_id, service, property_uri)
);
create index on connection_properties (organization_id, service);
create index on connection_properties using gin (matched_hosts);

-- ---------------------------------------------------------------------------
-- Which resource feeds which website.
-- ---------------------------------------------------------------------------
create table website_connections (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id) on delete cascade,
    website_id         uuid not null references websites(id) on delete cascade,
    property_id     uuid not null references connection_properties(id) on delete cascade,
    provider_key    text not null references providers(key),
    service         text not null,
    -- How the link was made, so a bad auto-match is diagnosable months later.
    link_method     text not null default 'auto'
                         check (link_method in ('auto','user_selected')),
    status          text not null default 'active'
                         check (status in ('active','paused','error','unlinked')),
    backfill_started_at   timestamptz,
    backfill_completed_at timestamptz,
    last_synced_date      date,
    last_error      text,
    created_at      timestamptz not null default now()
);
-- One ACTIVE link per (website, service), while still allowing a history of
-- unlinked or errored rows. A plain unique on (website_id, service, status) would
-- also forbid a second 'unlinked' row, which is not the intent.
create unique index site_connections_one_active
    on website_connections (website_id, service) where status = 'active';
create index on website_connections (website_id);

-- GA4 "key events" are arbitrary per property, so the platform cannot infer
-- which one means contact / purchase / donation. The wizard asks; the answer
-- lives here. Without it there is traffic data and no outcome data.
create table ga4_goal_events (
    id          uuid primary key default gen_random_uuid(),
    organization_id      uuid not null references organizations(id) on delete cascade,
    website_id     uuid not null references websites(id) on delete cascade,
    event_name  text not null,
    label       text not null,
    goal_kind   text not null default 'other'
                     check (goal_kind in ('contact','purchase','donation','signup','other')),
    is_primary  boolean not null default false,
    created_at  timestamptz not null default now(),
    unique (website_id, event_name)
);

-- ---------------------------------------------------------------------------
-- Sync bookkeeping. Every provider pull is recorded, so a partial sync is
-- visible and resumable rather than a silent hole in a trend chart.
-- ---------------------------------------------------------------------------
create table sync_runs (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id) on delete cascade,
    website_id         uuid references websites(id) on delete cascade,
    site_connection_id uuid references website_connections(id) on delete cascade,
    provider_key    text not null,
    service         text not null,
    kind            text not null check (kind in ('backfill','incremental','discovery')),
    range_start     date,
    range_end       date,
    status          text not null default 'running'
                         check (status in ('running','succeeded','partial','failed')),
    rows_written    bigint not null default 0,
    api_calls       int    not null default 0,
    quota_hits      int    not null default 0,
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    error           text
);
create index on sync_runs (website_id, service, started_at desc);
