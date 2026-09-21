-- 0014_partition_privileges.sql
--
-- Creating a partition requires ownership of the parent table, which the
-- service role deliberately does not have. But the 16-month backfill runs as
-- the service role and must provision the months it is about to write, because
-- a missing partition is an INSERT failure and the scheduler only ever looks
-- forwards.
--
-- So the partition helpers become SECURITY DEFINER: they run with the owner's
-- rights, and the service role is granted execute on them. That is a narrow,
-- auditable hole — "you may create partitions of these specific tables" —
-- rather than making the service role a table owner.
--
-- Two things keep it narrow:
--
--   1. `search_path` is pinned, so a caller cannot shadow a function or table
--      name the body resolves. An unpinned SECURITY DEFINER function is a
--      classic privilege-escalation bug.
--
--   2. The parent table is checked against an allowlist. `format('%I')` already
--      prevents SQL injection, but without this a caller could still create
--      partitions of any partitioned table in the database.

create or replace function app.partitionable_tables() returns text[]
language sql immutable as $$
    select array[
        'gsc_query_daily','gsc_page_daily','gsc_query_page_daily',
        'ga4_page_daily','ga4_goal_daily','ga4_dimension_daily',
        'page_snapshots'
    ];
$$;

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
    end if;
end $$;

create or replace function app.ensure_partitions_for_range(
    parent text, range_start date, range_end date
) returns int
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    cursor_month date := date_trunc('month', range_start)::date;
    last_month   date := date_trunc('month', range_end)::date;
    created int := 0;
    child text;
begin
    while cursor_month <= last_month loop
        child := format('%s_%s', parent, to_char(cursor_month, 'YYYYMM'));
        if to_regclass(child) is null then
            perform app.ensure_monthly_partition(parent, cursor_month);
            created := created + 1;
        end if;
        cursor_month := (cursor_month + interval '1 month')::date;
    end loop;
    return created;
end $$;

create or replace function app.ensure_partitions_for_backfill(
    range_start date, range_end date default current_date
) returns int
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    p text;
    created int := 0;
begin
    foreach p in array app.partitionable_tables() loop
        -- page_snapshots is partitioned by a timestamp, not a date, and is
        -- written by the crawler rather than a backfill.
        if p = 'page_snapshots' then
            continue;
        end if;
        created := created
                 + app.ensure_partitions_for_range(p, range_start, range_end);
    end loop;
    return created;
end $$;

-- Only the roles that run syncs need these.
revoke all on function app.ensure_monthly_partition(text, date) from public;
revoke all on function app.ensure_partitions_for_range(text, date, date) from public;
revoke all on function app.ensure_partitions_for_backfill(date, date) from public;

do $$ begin
    if exists (select 1 from pg_roles where rolname = 'app_service') then
        grant execute on function app.ensure_monthly_partition(text, date)
            to app_service;
        grant execute on function app.ensure_partitions_for_range(text, date, date)
            to app_service;
        grant execute on function app.ensure_partitions_for_backfill(date, date)
            to app_service;
    end if;
end $$;
