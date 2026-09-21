-- db/tests/smoke.sql
-- Proves the four schema claims that the design leans on. Run against a
-- database with all migrations applied. Every assertion raises on failure.

\set ON_ERROR_STOP on

-- Roles are cluster-wide, so this must be idempotent across re-runs.
do $$ begin
    if not exists (select 1 from pg_roles where rolname = 'app_user') then
        create role app_user nologin;
    end if;
end $$;
grant usage on schema public, app to app_user;
grant select, insert, update, delete on all tables in schema public to app_user;
grant execute on all functions in schema app to app_user;

-- Fixtures: two orgs that must never see each other.
insert into orgs (id, name, slug) values
  ('11111111-1111-1111-1111-111111111111','Org A','org-a'),
  ('22222222-2222-2222-2222-222222222222','Org B','org-b');
insert into profiles (id, email) values
  ('aaaaaaaa-0000-0000-0000-000000000001','a@example.com'),
  ('bbbbbbbb-0000-0000-0000-000000000002','b@example.com');
insert into org_members (org_id, user_id, role) values
  ('11111111-1111-1111-1111-111111111111','aaaaaaaa-0000-0000-0000-000000000001','owner'),
  ('22222222-2222-2222-2222-222222222222','bbbbbbbb-0000-0000-0000-000000000002','owner');
insert into sites (id, org_id, domain, origin) values
  ('5117e000-0000-0000-0000-00000000000a','11111111-1111-1111-1111-111111111111',
   'example.com','https://www.example.com'),
  ('5117e000-0000-0000-0000-00000000000b','22222222-2222-2222-2222-222222222222',
   'other.com','https://other.com');

-- ---------------------------------------------------------------------------
-- 1. Tenant isolation: org A's user sees exactly one site.
-- ---------------------------------------------------------------------------
set role app_user;
set app.user_id = 'aaaaaaaa-0000-0000-0000-000000000001';
do $$
declare n int; d text;
begin
    select count(*), min(domain::text) into n, d from sites;
    if n <> 1 or d <> 'example.com' then
        raise exception 'RLS FAIL: org A saw % site(s): %', n, d;
    end if;
    raise notice 'PASS  tenant isolation: org A sees only example.com';
end $$;
reset role;
reset app.user_id;

-- ---------------------------------------------------------------------------
-- 2. Position must be impressions-weighted, not a plain average.
--    Two days: position 3 on 1000 impressions, position 20 on 10.
--    Plain avg   = 11.5   (wrong, and plausible-looking)
--    Weighted    = 3.17   (correct)
-- ---------------------------------------------------------------------------
insert into gsc_query_daily
  (org_id, site_id, date, query_hash, query, clicks, impressions, position)
values
  ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
   current_date - 1, sha256('church in london'), 'church in london', 40, 1000, 3.0),
  ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
   current_date - 2, sha256('church in london'), 'church in london', 0, 10, 20.0);

do $$
declare weighted numeric; naive numeric;
begin
    select position into weighted from gsc_query_rollup
     where site_id = '5117e000-0000-0000-0000-00000000000a';
    select avg(position) into naive from gsc_query_daily
     where site_id = '5117e000-0000-0000-0000-00000000000a';
    if round(weighted, 2) <> 3.17 then
        raise exception 'WEIGHTING FAIL: got %, expected 3.17', round(weighted, 2);
    end if;
    raise notice 'PASS  weighted position % vs naive avg % (the naive figure is the bug)',
                 round(weighted, 2), round(naive, 2);
end $$;

-- ---------------------------------------------------------------------------
-- 3. The anonymised-clicks gap is visible rather than silently inconsistent.
--    Site total says 100 clicks; query rows only account for 40.
-- ---------------------------------------------------------------------------
insert into gsc_daily_totals (org_id, site_id, date, clicks, impressions, position)
values ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
        current_date - 1, 100, 5000, 8.4);

do $$
declare anon int; share numeric;
begin
    select anonymised_clicks, anonymised_share into anon, share
      from gsc_anonymised_share
     where site_id = '5117e000-0000-0000-0000-00000000000a'
       and date = current_date - 1;
    if anon <> 60 then
        raise exception 'ANONYMISED FAIL: got %, expected 60', anon;
    end if;
    raise notice 'PASS  anonymised gap surfaced: % clicks withheld by Google (% percent)',
                 anon, round(share * 100);
end $$;

-- ---------------------------------------------------------------------------
-- 4. The frontier leases without double-serving, and a dead lease returns.
-- ---------------------------------------------------------------------------
insert into crawls (id, org_id, site_id, trigger, status)
values ('c0000000-0000-0000-0000-00000000000c',
        '11111111-1111-1111-1111-111111111111',
        '5117e000-0000-0000-0000-00000000000a','manual','running');
insert into crawl_frontier (crawl_id, url_hash, url, depth)
select 'c0000000-0000-0000-0000-00000000000c', sha256(convert_to(u,'UTF8')), u, 0
from unnest(array['https://www.example.com/','https://www.example.com/about',
                  'https://www.example.com/contact']) u;

do $$
declare leased int; still_pending int;
begin
    with lease as (
        update crawl_frontier f
        set state = 'leased', leased_until = now() + interval '5 minutes',
            attempts = attempts + 1
        where (f.crawl_id, f.url_hash) in (
            select crawl_id, url_hash from crawl_frontier
            where crawl_id = 'c0000000-0000-0000-0000-00000000000c'
              and state = 'pending'
            order by depth, url_hash limit 2
            for update skip locked)
        returning 1)
    select count(*) into leased from lease;

    select count(*) into still_pending from crawl_frontier
     where crawl_id = 'c0000000-0000-0000-0000-00000000000c' and state = 'pending';

    if leased <> 2 or still_pending <> 1 then
        raise exception 'FRONTIER FAIL: leased %, pending %', leased, still_pending;
    end if;

    -- Simulate a killed worker: its lease expires and the row is reclaimable.
    update crawl_frontier set leased_until = now() - interval '1 minute'
     where crawl_id = 'c0000000-0000-0000-0000-00000000000c' and state = 'leased';
    update crawl_frontier set state = 'pending'
     where crawl_id = 'c0000000-0000-0000-0000-00000000000c'
       and state = 'leased' and leased_until < now();

    select count(*) into still_pending from crawl_frontier
     where crawl_id = 'c0000000-0000-0000-0000-00000000000c' and state = 'pending';
    if still_pending <> 3 then
        raise exception 'RECLAIM FAIL: expected 3 pending, got %', still_pending;
    end if;
    raise notice 'PASS  frontier leased 2 of 3, and a dead lease was reclaimed';
end $$;

-- ---------------------------------------------------------------------------
-- 5. Only one active Google link per (site, service).
-- ---------------------------------------------------------------------------
insert into google_accounts (id, org_id, google_sub, email)
values ('9a000000-0000-0000-0000-00000000000a',
        '11111111-1111-1111-1111-111111111111','sub-123','a@example.com');
insert into google_resources (id, org_id, account_id, service, resource_uri, matched_hosts)
values ('9e000000-0000-0000-0000-000000000001','11111111-1111-1111-1111-111111111111',
        '9a000000-0000-0000-0000-00000000000a','search_console',
        'sc-domain:example.com','{example.com}'),
       ('9e000000-0000-0000-0000-000000000002','11111111-1111-1111-1111-111111111111',
        '9a000000-0000-0000-0000-00000000000a','search_console',
        'https://www.example.com/','{www.example.com}');
insert into site_google_links (org_id, site_id, resource_id, service)
values ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
        '9e000000-0000-0000-0000-000000000001','search_console');

do $$
begin
    begin
        insert into site_google_links (org_id, site_id, resource_id, service)
        values ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
                '9e000000-0000-0000-0000-000000000002','search_console');
        raise exception 'LINK FAIL: a second active link was allowed';
    exception when unique_violation then
        raise notice 'PASS  second active search_console link rejected';
    end;
    -- but an unlinked historical row is still permitted
    insert into site_google_links (org_id, site_id, resource_id, service, status)
    values ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
            '9e000000-0000-0000-0000-000000000002','search_console','unlinked');
    raise notice 'PASS  historical unlinked row still permitted';
end $$;
