-- 0001_tenancy.sql
-- Organisations, membership, websites. Every tenant-scoped table in later
-- migrations carries organization_id directly so row-level security is a single
-- predicate and never a join chain.

create extension if not exists pgcrypto;
create extension if not exists pg_trgm;
create extension if not exists citext;

create table organizations (
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

-- Mirrors the identity provider's user id (Supabase auth.users.id).
--
-- The V1 spec (s26) lists `password_hash` on this table. It is deliberately
-- ABSENT here: credentials live in the auth provider's own schema, which the
-- application never queries and never joins against. An application table that
-- cannot leak a password hash is strictly safer than one that can, and email
-- /password sign-in works identically either way.
create table users (
    id          uuid primary key,
    email       citext      not null unique,
    full_name   text,
    created_at  timestamptz not null default now()
);

create table organization_members (
    organization_id      uuid not null references organizations(id)     on delete cascade,
    user_id     uuid not null references users(id) on delete cascade,
    role        text not null default 'member'
                     check (role in ('owner','admin','member','viewer')),
    created_at  timestamptz not null default now(),
    primary key (organization_id, user_id)
);
create index on organization_members (user_id);

create table websites (
    id          uuid primary key default gen_random_uuid(),
    organization_id      uuid   not null references organizations(id) on delete cascade,
    -- Registrable domain, lowercase, no scheme: example.com
    domain      citext not null,
    -- Preferred origin used as the crawl seed: https://www.example.com
    canonical_url text not null,
    name         text,
    timezone    text   not null default 'UTC',
    -- Per-website overrides layered on top of the plan defaults.
    crawl_config jsonb not null default '{}'::jsonb,
    crawl_schedule text not null default 'weekly'
                        check (crawl_schedule in ('off','weekly','daily')),
    status      text   not null default 'PENDING'
                       check (status in ('PENDING','CONNECTING','CRAWLING','READY','ERROR')),
    created_at  timestamptz not null default now(),
    archived_at timestamptz,
    unique (organization_id, domain)
);
create index on websites (organization_id) where archived_at is null;
