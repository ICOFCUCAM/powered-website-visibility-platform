-- 0002_google_hub.sql
-- The Google Hub: one connected Google ACCOUNT can grant many SERVICES, and
-- each service exposes many RESOURCES (a GSC property, a GA4 property, a
-- Business Profile location). Modelling it as account -> resource -> link is
-- what makes "Continue with Google" able to discover everything at once and
-- what lets the Hub stand alone as a product.

-- ---------------------------------------------------------------------------
-- Token vault. Deliberately a separate schema: refresh tokens are the crown
-- jewels and must never be reachable from an ordinary tenant query, a view, an
-- ORM model or a PostgREST/Supabase client. No RLS policy is defined here,
-- which means no anon/authenticated role can read it at all. Only the backend
-- service role touches this schema.
-- ---------------------------------------------------------------------------
create schema if not exists secrets;
revoke all on schema secrets from public;

create table secrets.oauth_tokens (
    id              uuid primary key default gen_random_uuid(),
    -- Envelope encryption: ciphertext here, data key wrapped by a KMS key.
    -- The application never stores a plaintext token or the master key.
    ciphertext      bytea       not null,
    wrapped_dek     bytea       not null,
    key_version     int         not null default 1,
    algo            text        not null default 'AES-256-GCM',
    nonce           bytea       not null,
    created_at      timestamptz not null default now(),
    rotated_at      timestamptz
);

-- ---------------------------------------------------------------------------
-- Connected Google accounts
-- ---------------------------------------------------------------------------
create table google_accounts (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid   not null references orgs(id) on delete cascade,
    -- Google's stable subject id. NEVER key on email: users change it.
    google_sub      text   not null,
    email           citext not null,
    picture_url     text,
    -- Scopes actually granted, which may be fewer than those requested.
    -- Incremental authorisation means this grows over time.
    granted_scopes  text[] not null default '{}',
    refresh_token_id uuid  references secrets.oauth_tokens(id) on delete set null,
    status          text   not null default 'active'
                           check (status in ('active','needs_reauth','revoked','error')),
    last_error      text,
    last_refreshed_at timestamptz,
    connected_by    uuid   references profiles(id) on delete set null,
    created_at      timestamptz not null default now(),
    revoked_at      timestamptz,
    unique (org_id, google_sub)
);
create index on google_accounts (org_id) where status = 'active';

-- ---------------------------------------------------------------------------
-- Everything discovered after consent, across every service, in one table.
-- Populated by sites.list (GSC), accountSummaries (GA4), accounts.locations
-- (GBP). This is what the wizard's "Choose your website" step renders.
-- ---------------------------------------------------------------------------
create table google_resources (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null references orgs(id) on delete cascade,
    account_id      uuid not null references google_accounts(id) on delete cascade,
    service         text not null
                         check (service in ('search_console','analytics','business_profile','ads')),
    -- Service-native identifier, stored verbatim:
    --   search_console    sc-domain:example.com | https://www.example.com/
    --   analytics         properties/123456789
    --   business_profile  locations/0123456789
    --   ads               customers/1234567890
    resource_uri    text not null,
    display_name    text,
    -- GSC: siteOwner|siteFullUser|siteRestrictedUser|siteUnverifiedUser
    -- GA4: the effective role returned by accountSummaries
    permission_level text,
    -- Normalised host(s) this resource is about, used for auto-matching a
    -- resource to a site. GSC domain properties expand to the bare domain.
    matched_hosts   text[] not null default '{}',
    raw             jsonb  not null default '{}'::jsonb,
    discovered_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),
    unique (account_id, service, resource_uri)
);
create index on google_resources (org_id, service);
create index on google_resources using gin (matched_hosts);

-- ---------------------------------------------------------------------------
-- Which resource feeds which site. One active link per (site, service).
-- ---------------------------------------------------------------------------
create table site_google_links (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null references orgs(id) on delete cascade,
    site_id         uuid not null references sites(id) on delete cascade,
    resource_id     uuid not null references google_resources(id) on delete cascade,
    service         text not null
                         check (service in ('search_console','analytics','business_profile','ads')),
    -- How the link was made, for debugging bad auto-matches.
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
-- One ACTIVE link per (site, service), while still allowing a history of
-- unlinked/errored rows. A plain unique on (site_id, service, status) would
-- also forbid a second 'unlinked' row, which is not the intent.
create unique index site_google_links_one_active
    on site_google_links (site_id, service) where status = 'active';
create index on site_google_links (site_id);

-- GA4 conversion mapping. GA4 "key events" are arbitrary per property, so the
-- platform cannot infer which one means contact / purchase / donation. The
-- wizard asks, and the answer lives here. Without it there is traffic data and
-- no outcome data.
create table ga4_goal_events (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null references orgs(id) on delete cascade,
    site_id     uuid not null references sites(id) on delete cascade,
    event_name  text not null,
    label       text not null,
    goal_kind   text not null default 'other'
                     check (goal_kind in ('contact','purchase','donation','signup','other')),
    is_primary  boolean not null default false,
    created_at  timestamptz not null default now(),
    unique (site_id, event_name)
);

-- ---------------------------------------------------------------------------
-- Sync bookkeeping. Every API pull is recorded so a failed or partial sync is
-- visible and resumable rather than silently leaving a hole in a trend chart.
-- ---------------------------------------------------------------------------
create table google_sync_runs (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null references orgs(id) on delete cascade,
    site_id         uuid references sites(id) on delete cascade,
    link_id         uuid references site_google_links(id) on delete cascade,
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
create index on google_sync_runs (site_id, service, started_at desc);
