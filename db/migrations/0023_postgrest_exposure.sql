-- 0023_postgrest_exposure.sql
-- Take the browser-facing roles off the schema.
--
-- Only relevant on a managed Postgres that fronts the database with PostgREST
-- — Supabase being the one this project deploys to. There, `anon` and
-- `authenticated` are granted SELECT/INSERT/UPDATE/DELETE on every table
-- created in `public` by ALTER DEFAULT PRIVILEGES, and PostgREST exposes each
-- one at /rest/v1/<table> to anybody holding the publishable anon key.
--
-- RLS covers the tenant tables. It did NOT cover:
--
--   * the monthly partitions. A partition carries no policies of its own, so
--     `POST /rest/v1/gsc_query_daily_202609` wrote straight into a table whose
--     parent is protected. Thirty-five of them existed; a new one appears
--     every month.
--   * `providers` and `provider_services`, created in 0002 before the
--     catalogue pattern in 0007, with RLS never enabled.
--   * `issue_explanations`, created in 0017, likewise.
--   * six views, which ran as their owner and so bypassed RLS for whoever
--     queried them.
--
-- This application never uses PostgREST: it connects with psycopg as app_user
-- or app_service. So the fix is not to patch each table but to remove the
-- roles' access to the schema, which also covers every table added later.

begin;

-- -- 1. Now ------------------------------------------------------------------
do $$ begin
    if not exists (select 1 from pg_roles where rolname = 'anon') then
        -- Not a PostgREST deployment; nothing here applies.
        return;
    end if;

    revoke all on all tables    in schema public  from anon, authenticated;
    revoke all on all sequences in schema public  from anon, authenticated;
    revoke all on all functions in schema public  from anon, authenticated;
    revoke all on schema public  from anon, authenticated;

    revoke all on all tables    in schema secrets from anon, authenticated;
    revoke all on schema secrets from anon, authenticated;

    revoke all on all tables    in schema app     from anon, authenticated;
    revoke all on all functions in schema app     from anon, authenticated;
    revoke all on schema app     from anon, authenticated;
end $$;

-- A function grants EXECUTE to PUBLIC by default, which `anon` inherits. The
-- schema revoke above already makes them unreachable; this removes the grant
-- as well so the two are not relied upon to agree.
revoke execute on all functions in schema app from public;
grant  execute on all functions in schema app to app_user, app_service;

-- -- 2. And for everything created later --------------------------------------
--
-- Without this the hole reopens on the first of the month, when
-- app.ensure_monthly_partition creates the next partition.
do $$ begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        alter default privileges in schema public
            revoke all on tables from anon, authenticated;
        alter default privileges in schema public
            revoke all on sequences from anon, authenticated;
    end if;
end $$;
alter default privileges in schema app revoke execute on functions from public;

-- -- 3. RLS on the partitions themselves ---------------------------------------
--
-- Querying through the parent applies the PARENT's policies, so this costs the
-- application nothing — verified against a live database: a member still reads
-- their row through `gsc_query_daily`, a non-member still reads none. What it
-- blocks is addressing a partition directly by name.
--
-- These deliberately get no policy. test_rls_backstop.py flags "RLS enabled
-- with no policy" as a silent failure, and scopes that check to
-- `relispartition = false` precisely because for a partition it is the
-- intended state.
do $$
declare child text;
begin
    for child in
        select c.relname
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relkind = 'r' and c.relispartition
    loop
        execute format('alter table public.%I enable row level security', child);
    end loop;
end $$;

-- -- 4. The three tables that never picked up the catalogue pattern ------------
--
-- A `using (true)` policy is not protection; step 1 is. It makes their state
-- explicit rather than absent, and satisfies the backstop test's rule that a
-- table with RLS enabled must also carry a policy.
alter table providers         enable row level security;
alter table provider_services enable row level security;
drop policy if exists providers_read         on providers;
drop policy if exists provider_services_read on provider_services;
create policy providers_read         on providers         for select using (true);
create policy provider_services_read on provider_services for select using (true);

-- Global by design: its key is a hash of the complete prompt payload, so a
-- shared row cannot carry one tenant's data to another. The permissive policy
-- preserves exactly the access the application has today.
alter table issue_explanations enable row level security;
drop policy if exists issue_explanations_all on issue_explanations;
create policy issue_explanations_all on issue_explanations
    for all using (true) with check (true);

-- -- 5. Views run as the caller -------------------------------------------------
--
-- They ran as their owner, so every one of them bypassed row-level security
-- for whoever queried it — which is the opposite of what the RLS design in
-- 0006 assumes. app_service still sees everything; it bypasses RLS by role.
alter view gsc_query_rollup           set (security_invoker = true);
alter view gsc_page_rollup            set (security_invoker = true);
alter view gsc_anonymised_share       set (security_invoker = true);
alter view page_current               set (security_invoker = true);
alter view provenance_index           set (security_invoker = true);
alter view website_ownership_evidence set (security_invoker = true);

-- -- 6. Pin the search_path on the remaining functions --------------------------
--
-- 0014 set it on the SECURITY DEFINER partition helpers. These are the rest.
alter function app.current_user_id()               set search_path = pg_catalog, public;
alter function app.property_covers_url(text, text) set search_path = pg_catalog, public;
alter function app.partitionable_tables()          set search_path = pg_catalog;
alter function app.lock_derived_columns()          set search_path = public, pg_catalog, pg_temp;
alter function app.ensure_partitions_ahead(int)    set search_path = public, app, pg_temp;

-- -- 7. A partition created next month must be born locked ----------------------
create or replace function app.ensure_monthly_partition(
    parent text, month_start date
) returns void
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    child text := format('%s_%s', parent, to_char(month_start, 'YYYYMM'));
begin
    if not (parent = any (app.partitionable_tables())) then
        raise exception 'refusing to partition %, which is not on the allowlist',
            parent using errcode = 'insufficient_privilege';
    end if;
    if to_regclass(child) is null then
        execute format(
            'create table %I partition of %I for values from (%L) to (%L)',
            child, parent, month_start, (month_start + interval '1 month')::date);

        -- Free, because access through the parent uses the parent's policies.
        -- What it stops is PostgREST addressing the new partition by name on
        -- the morning it appears.
        execute format('alter table %I enable row level security', child);
        if exists (select 1 from pg_roles where rolname = 'anon') then
            execute format('revoke all on %I from anon, authenticated', child);
        end if;
    end if;
end $$;

revoke all on function app.ensure_monthly_partition(text, date) from public;
grant execute on function app.ensure_monthly_partition(text, date) to app_service;

commit;
