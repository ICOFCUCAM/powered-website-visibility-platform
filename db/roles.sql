-- db/roles.sql
-- Database roles. Not a migration: roles are cluster-wide, migrations are
-- per-database. Run once per cluster, and re-run after adding tables.
--
-- The API connects as `app_user`, deliberately NOT as the owner and never as a
-- superuser. Row-level security is bypassed entirely by superusers and would
-- be bypassed by the table owner without `force row level security`. Connecting
-- as an unprivileged role is what makes the RLS policies in 0006 load-bearing
-- rather than decorative.

do $$ begin
    if not exists (select 1 from pg_roles where rolname = 'app_user') then
        create role app_user nologin;
    end if;
end $$;

grant usage on schema public, app to app_user;
grant select, insert, update, delete on all tables in schema public to app_user;
grant usage, select on all sequences in schema public to app_user;
grant execute on all functions in schema app to app_user;

-- `secrets` is deliberately absent: no grant, no policy, no path from a client
-- role to a customer's refresh token.

-- Re-apply the derived-column protection. A table-level GRANT above re-grants
-- every column, which would silently undo migration 0012.
select app.lock_derived_columns();
