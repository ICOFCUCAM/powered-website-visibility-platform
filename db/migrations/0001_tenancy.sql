-- 0001_tenancy.sql
-- Organisations, membership, sites. Every tenant-scoped table in later
-- migrations carries org_id directly so row-level security is a single
-- predicate and never a join chain.

create extension if not exists pgcrypto;
create extension if not exists pg_trgm;
create extension if not exists citext;

create table orgs (
    id          uuid primary key default gen_random_uuid(),
    name        text        not null,
    slug        citext      not null unique,
    plan        text        not null default 'free'
                            check (plan in ('free','pro','agency','enterprise')),
    -- Hard caps enforced at job-admission time, not at render time.
    max_sites            int     not null default 1,
    max_pages_per_crawl  int     not null default 100,
    monthly_ai_budget_usd numeric(10,2) not null default 2.00,
    created_at  timestamptz not null default now()
);

-- Mirrors the identity provider's user id (Supabase auth.users.id, or your own
-- users table). Application data never lives in the auth schema.
create table profiles (
    id          uuid primary key,
    email       citext      not null unique,
    full_name   text,
    created_at  timestamptz not null default now()
);

create table org_members (
    org_id      uuid not null references orgs(id)     on delete cascade,
    user_id     uuid not null references profiles(id) on delete cascade,
    role        text not null default 'member'
                     check (role in ('owner','admin','member','viewer')),
    created_at  timestamptz not null default now(),
    primary key (org_id, user_id)
);
create index on org_members (user_id);

create table sites (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid   not null references orgs(id) on delete cascade,
    -- Registrable domain, lowercase, no scheme: example.com
    domain      citext not null,
    -- Preferred origin used as the crawl seed: https://www.example.com
    origin      text   not null,
    display_name text,
    timezone    text   not null default 'UTC',
    -- Per-site overrides layered on top of the plan defaults.
    crawl_config jsonb not null default '{}'::jsonb,
    crawl_schedule text not null default 'weekly'
                        check (crawl_schedule in ('off','weekly','daily')),
    onboarding_state text not null default 'created'
                        check (onboarding_state in
                               ('created','google_linked','crawling','ready')),
    created_at  timestamptz not null default now(),
    archived_at timestamptz,
    unique (org_id, domain)
);
create index on sites (org_id) where archived_at is null;
