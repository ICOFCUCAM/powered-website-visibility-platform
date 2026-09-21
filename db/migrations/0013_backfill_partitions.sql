-- 0013_backfill_partitions.sql
--
-- `ensure_partitions_ahead` keeps three months in front of today, which is
-- right for ongoing sync and wrong for the first thing a new account does: a
-- 16-month Search Console backfill writes rows for months that have no
-- partition, and a missing partition is an INSERT failure, not a silent drop.
--
-- So the backfill creates what it needs before it writes, rather than relying
-- on a scheduler that only ever looks forwards.

create or replace function app.ensure_partitions_for_range(
    parent text, range_start date, range_end date
) returns int
language plpgsql as $$
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

-- Everything a Search Console or Analytics backfill touches, in one call.
create or replace function app.ensure_partitions_for_backfill(
    range_start date, range_end date default current_date
) returns int
language plpgsql as $$
declare
    p text;
    created int := 0;
begin
    foreach p in array array[
        'gsc_query_daily','gsc_page_daily','gsc_query_page_daily',
        'ga4_page_daily','ga4_goal_daily','ga4_dimension_daily'
    ] loop
        created := created
                 + app.ensure_partitions_for_range(p, range_start, range_end);
    end loop;
    return created;
end $$;

comment on function app.ensure_partitions_for_backfill(date, date) is
    'Called before a backfill writes. Idempotent: existing partitions are left alone.';
