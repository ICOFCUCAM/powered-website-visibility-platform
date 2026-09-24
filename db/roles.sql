-- db/roles.sql
-- Database roles. Not a migration: roles are cluster-wide, migrations are
-- per-database. Run once per cluster, and re-run after adding tables.
--
-- TWO login roles, because they need opposite things from row-level security.
--
--   app_user     the request path. RLS APPLIES. No access to `secrets`.
--                Every user-driven read runs as this role, so a handler that
--                forgets a tenant filter still cannot return another
--                organisation's rows.
--
--   app_service  background work and the token vault. BYPASSRLS, because a
--                sync worker legitimately operates across organisations and
--                has no `app.user_id` to bind. Never used to serve a request
--                whose shape is chosen by a user.
--
-- Connecting the API as the database owner or a superuser would silently
-- bypass every policy in migration 0006 and make them decorative. That is
-- exactly the mistake this file exists to prevent.

do $$ begin
    if not exists (select 1 from pg_roles where rolname = 'app_user') then
        create role app_user login;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'app_service') then
        create role app_service login bypassrls;
    end if;
end $$;

-- Ensure the attributes are right even if the roles predate this file. Roles
-- are cluster-wide and survive a database drop, so a role created earlier with
-- different attributes must be corrected rather than assumed.
alter role app_user    login nobypassrls;
alter role app_service login bypassrls;

-- -- request path -----------------------------------------------------------
grant usage on schema public, app to app_user;
grant select, insert, update, delete on all tables in schema public to app_user;
grant usage, select on all sequences in schema public to app_user;
grant execute on all functions in schema app to app_user;

-- `secrets` is deliberately absent: no grant, no policy, no path from the
-- request-path role to a customer's refresh token.
revoke all on schema secrets from app_user;

-- -- service path -----------------------------------------------------------
grant usage on schema public, app, secrets to app_service;
grant select, insert, update, delete on all tables in schema public to app_service;
grant select, insert, update, delete on all tables in schema secrets to app_service;
grant usage, select on all sequences in schema public to app_service;
grant execute on all functions in schema app to app_service;

-- -- browser-facing roles --------------------------------------------------
--
-- On a managed Postgres that fronts the database with PostgREST — Supabase,
-- which this project deploys to — `anon` and `authenticated` are granted on
-- every table created in `public` by ALTER DEFAULT PRIVILEGES, and each table
-- is exposed at /rest/v1/<table> to anybody holding the publishable key.
--
-- This file is documented as "re-run after adding tables", so it is exactly
-- where that grant has to be taken away again: a new table added tomorrow
-- arrives reachable, and a re-run that only tops up app_user would leave it
-- that way. See 0023_postgrest_exposure.sql for the full reasoning.
do $$ begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on all tables    in schema public  from anon, authenticated;
        revoke all on all sequences in schema public  from anon, authenticated;
        revoke all on all functions in schema public  from anon, authenticated;
        revoke all on schema public  from anon, authenticated;

        revoke all on all tables    in schema secrets from anon, authenticated;
        revoke all on schema secrets from anon, authenticated;

        revoke all on all tables    in schema app     from anon, authenticated;
        revoke all on all functions in schema app     from anon, authenticated;
        revoke all on schema app     from anon, authenticated;
    end if;
end $$;

-- EXECUTE defaults to PUBLIC, which `anon` inherits. The schema revoke above
-- already makes these unreachable; this stops the two having to agree.
revoke execute on all functions in schema app from public;
grant  execute on all functions in schema app to app_user, app_service;

-- Re-apply the derived-column protection. A table-level GRANT above re-grants
-- every column, which would silently undo migration 0012.
select app.lock_derived_columns();
