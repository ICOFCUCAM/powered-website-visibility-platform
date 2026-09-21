-- 0006_rls.sql
-- Row-level security and partition management.
--
-- Every tenant table carries organization_id directly, so each policy is one indexed
-- predicate instead of a join chain. `secrets` has NO policies and NO grants:
-- refresh tokens are unreachable from any client role by construction.

create schema if not exists app;

-- Resolves the caller. Supabase supplies auth.uid(); a self-hosted deployment
-- sets `app.user_id` as a GUC per connection. Both paths land here so the
-- policies below never need to know which deployment they are running in.
create or replace function app.current_user_id() returns uuid
language plpgsql stable as $$
declare uid uuid;
begin
    begin
        uid := nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
    exception when others then uid := null;
    end;
    if uid is null then
        begin
            uid := nullif(current_setting('app.user_id', true), '')::uuid;
        exception when others then uid := null;
        end;
    end if;
    return uid;
end $$;

create or replace function app.is_org_member(target_org uuid) returns boolean
language sql stable security definer set search_path = public, app as $$
    select exists (
        select 1 from organization_members m
        where m.organization_id = target_org
          and m.user_id = app.current_user_id()
    );
$$;

create or replace function app.can_write_org(target_org uuid) returns boolean
language sql stable security definer set search_path = public, app as $$
    select exists (
        select 1 from organization_members m
        where m.organization_id = target_org
          and m.user_id = app.current_user_id()
          and m.role in ('owner','admin','member')
    );
$$;

-- Apply the standard policy set to every tenant table that has an organization_id.
do $$
declare t text;
begin
    foreach t in array array[
        'websites','connections','connection_properties','website_connections',
        'ga4_goal_events','sync_runs','gsc_totals_daily','gsc_query_daily',
        'gsc_page_daily','gsc_query_page_daily','ga4_daily','ga4_page_daily',
        'ga4_goal_daily','crawls','pages','page_snapshots','psi_samples',
        'issues','keywords','score_snapshots','plans','recommendations',
        'actions','reports'
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

-- Org and membership rows are visible to members of that org only.
alter table organizations enable row level security;
create policy orgs_read on organizations for select using (app.is_org_member(id));

alter table organization_members enable row level security;
create policy org_members_read on organization_members for select
    using (app.is_org_member(organization_id));

alter table users enable row level security;
create policy profiles_self on users for select
    using (id = app.current_user_id());

-- Tables reached only through a parent (no organization_id of their own) stay closed to
-- client roles and are read through the API, which holds the service role.
alter table crawl_frontier     enable row level security;
alter table page_links         enable row level security;
alter table issue_observations enable row level security;
alter table llm_calls          enable row level security;
alter table issue_types        enable row level security;
create policy issue_types_read on issue_types for select using (true);

-- ---------------------------------------------------------------------------
-- Partition management
-- ---------------------------------------------------------------------------
-- Monthly range partitions for the high-volume tables. Called by a scheduled
-- job that keeps 3 months of partitions ahead; a missing partition is an
-- INSERT failure, not a silent drop, so this must never fall behind.
create or replace function app.ensure_monthly_partition(
    parent text, month_start date
) returns void
language plpgsql as $$
declare
    child text := format('%s_%s', parent, to_char(month_start, 'YYYYMM'));
begin
    if to_regclass(child) is null then
        execute format(
            'create table %I partition of %I for values from (%L) to (%L)',
            child, parent, month_start, (month_start + interval '1 month')::date);
    end if;
end $$;

create or replace function app.ensure_partitions_ahead(months int default 3)
returns void language plpgsql as $$
declare
    p text;
    i int;
    base date := date_trunc('month', current_date)::date;
begin
    foreach p in array array[
        'gsc_query_daily','gsc_page_daily','gsc_query_page_daily',
        'ga4_page_daily','ga4_goal_daily','page_snapshots'
    ] loop
        for i in -1 .. months loop
            perform app.ensure_monthly_partition(p, (base + (i || ' month')::interval)::date);
        end loop;
    end loop;
end $$;

select app.ensure_partitions_ahead(3);
