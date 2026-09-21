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

-- Re-apply the derived-column protection. A table-level GRANT above re-grants
-- every column, which would silently undo migration 0012.
select app.lock_derived_columns();
