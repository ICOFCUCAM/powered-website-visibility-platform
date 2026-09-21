-- 0012_derived_columns.sql
--
-- M1 DECISION, recorded:
--
--   crawl_allowed is DERIVED, NEVER USER-CONTROLLED, and enforced immediately
--   before crawl dispatch.
--
--   crawl_allowed = ownership_verified
--                   AND ownership coverage matches the crawl target
--                   AND website status is active
--
-- No database trigger. A trigger cannot know facts that live outside Postgres
-- (plan state, robots.txt, an operator suspension), so it would be either
-- incomplete or would reach further than a trigger should. The invariant is
-- evaluated at the service boundary:
--
--     request crawl -> load website -> evaluate crawl_allowed()
--                   -> false: reject, no job created
--                   -> true:  enqueue
--
-- and the crawler INDEPENDENTLY RE-CHECKS the prerequisites before it starts
-- fetching. Defence in depth: the gate that admits the job and the gate that
-- starts the work are separate, so a stale queue entry cannot crawl a website
-- whose verification was revoked in between.
--
-- What the database can do is make the field unwritable by clients. Under a
-- deployment where client roles reach Postgres directly (Supabase PostgREST),
-- "the API never sets this" is not enough — the column grant has to say so.

-- Mechanism note, because the obvious spelling does not work: a column-level
-- REVOKE does NOT subtract from a table-level GRANT UPDATE. Postgres treats
-- them as separate grants, and the table-wide one still permits every column.
-- The only way to protect a column is to revoke UPDATE on the table and then
-- grant it back column by column for everything else.
create or replace function app.lock_derived_columns() returns void
language plpgsql as $$
declare
    r    text;
    tbl  text;
    prot text[];
    allowed text;
    targets text[][] := array[
        array['websites',
              'crawl_allowed,crawl_blocked_reason,ownership_verified_at,'
              || 'ownership_method,ownership_property_id'],
        array['organizations',
              'plan,max_websites,max_pages_per_crawl,monthly_ai_budget_usd']
    ];
    i int;
begin
    -- app_service is deliberately absent: it writes these columns as the
    -- outcome of verification, which is the one legitimate path.
    foreach r in array array['anon','authenticated','app_user'] loop
        if not exists (select 1 from pg_roles where rolname = r) then
            continue;
        end if;
        for i in 1 .. array_length(targets, 1) loop
            tbl  := targets[i][1];
            prot := string_to_array(targets[i][2], ',');

            select string_agg(quote_ident(column_name), ', ')
              into allowed
              from information_schema.columns
             where table_schema = 'public'
               and table_name = tbl
               and not (column_name = any (prot));

            execute format('revoke update on %I from %I', tbl, r);
            if allowed is not null then
                execute format('grant update (%s) on %I to %I', allowed, tbl, r);
            end if;
        end loop;
    end loop;
end $$;

comment on function app.lock_derived_columns() is
    'Re-run after any blanket GRANT: a table-level GRANT UPDATE re-grants every column, silently undoing these revokes.';

select app.lock_derived_columns();

comment on column websites.crawl_allowed is
    'DERIVED, never client-supplied. Kept for observability; the authoritative check runs at crawl admission and again in the crawler before fetching.';
