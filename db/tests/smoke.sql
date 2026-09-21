-- db/tests/smoke.sql
-- Proves the four schema claims that the design leans on. Run against a
-- database with all migrations applied. Every assertion raises on failure.

\set ON_ERROR_STOP on

-- Roles come from db/roles.sql, which the runner applies first, so the tests
-- exercise exactly the grants a real deployment has.

-- Re-runnable: the fixtures use fixed ids, so a previous run is cleared
-- first. Everything tenant-scoped cascades from the organization rows.
delete from organizations where id in (
    '11111111-1111-1111-1111-111111111111',
    '22222222-2222-2222-2222-222222222222');
delete from users where id in (
    'aaaaaaaa-0000-0000-0000-000000000001',
    'bbbbbbbb-0000-0000-0000-000000000002');
delete from data_providers where key = 'example_vendor';
-- Partitioned fact tables carry organization_id but no foreign key (a
-- partitioned parent cannot cascade), so they are cleared by hand.
do $$
declare t text;
begin
    foreach t in array array[
        'gsc_query_daily','gsc_page_daily','gsc_query_page_daily',
        'ga4_page_daily','ga4_goal_daily','ga4_dimension_daily',
        'page_snapshots','llm_calls','audit_log','backlink_changes','alert_events'
    ] loop
        execute format(
            'delete from %I where organization_id in (%L, %L)', t,
            '11111111-1111-1111-1111-111111111111',
            '22222222-2222-2222-2222-222222222222');
    end loop;
end $$;

-- Fixtures: two organizations that must never see each other.
insert into organizations (id, name, slug) values
  ('11111111-1111-1111-1111-111111111111','Org A','org-a'),
  ('22222222-2222-2222-2222-222222222222','Org B','org-b');
insert into users (id, email) values
  ('aaaaaaaa-0000-0000-0000-000000000001','a@example.com'),
  ('bbbbbbbb-0000-0000-0000-000000000002','b@example.com');
insert into organization_members (organization_id, user_id, role) values
  ('11111111-1111-1111-1111-111111111111','aaaaaaaa-0000-0000-0000-000000000001','owner'),
  ('22222222-2222-2222-2222-222222222222','bbbbbbbb-0000-0000-0000-000000000002','owner');
insert into websites (id, organization_id, domain, canonical_url) values
  ('5117e000-0000-0000-0000-00000000000a','11111111-1111-1111-1111-111111111111',
   'example.com','https://www.example.com'),
  ('5117e000-0000-0000-0000-00000000000b','22222222-2222-2222-2222-222222222222',
   'other.com','https://other.com');

-- ---------------------------------------------------------------------------
-- 1. Tenant isolation: org A's user sees exactly one website.
-- ---------------------------------------------------------------------------
set role app_user;
set app.user_id = 'aaaaaaaa-0000-0000-0000-000000000001';
do $$
declare n int; d text;
begin
    select count(*), min(domain::text) into n, d from websites;
    if n <> 1 or d <> 'example.com' then
        raise exception 'RLS FAIL: org A saw % website(s): %', n, d;
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
  (organization_id, website_id, date, query_hash, query, clicks, impressions, position)
values
  ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
   current_date - 1, sha256('church in london'), 'church in london', 40, 1000, 3.0),
  ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
   current_date - 2, sha256('church in london'), 'church in london', 0, 10, 20.0);

do $$
declare weighted numeric; naive numeric;
begin
    select position into weighted from gsc_query_rollup
     where website_id = '5117e000-0000-0000-0000-00000000000a';
    select avg(position) into naive from gsc_query_daily
     where website_id = '5117e000-0000-0000-0000-00000000000a';
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
insert into gsc_totals_daily (organization_id, website_id, date, clicks, impressions, position)
values ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
        current_date - 1, 100, 5000, 8.4);

do $$
declare anon int; share numeric;
begin
    select anonymised_clicks, anonymised_share into anon, share
      from gsc_anonymised_share
     where website_id = '5117e000-0000-0000-0000-00000000000a'
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
insert into crawls (id, organization_id, website_id, trigger, status)
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
        set state = 'leased',
            worker_id = 'worker-a',
            leased_at = now(),
            lease_expires_at = now() + interval '5 minutes',
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

    -- worker-a dies. Its lease expires. The sweeper reclaims the rows WITHOUT
    -- consulting worker_id — recovery must never depend on knowing anything
    -- about the dead worker.
    update crawl_frontier set lease_expires_at = now() - interval '1 minute'
     where crawl_id = 'c0000000-0000-0000-0000-00000000000c' and state = 'leased';
    update crawl_frontier set state = 'pending', worker_id = null
     where crawl_id = 'c0000000-0000-0000-0000-00000000000c'
       and state = 'leased' and lease_expires_at < now();

    select count(*) into still_pending from crawl_frontier
     where crawl_id = 'c0000000-0000-0000-0000-00000000000c' and state = 'pending';
    if still_pending <> 3 then
        raise exception 'RECLAIM FAIL: expected 3 pending, got %', still_pending;
    end if;
    raise notice 'PASS  frontier leased 2 of 3, and a dead lease was reclaimed';
end $$;

-- A different worker can now claim what worker-a was holding.
do $$
declare claimed int;
begin
    with lease as (
        update crawl_frontier f
        set state = 'leased', worker_id = 'worker-b', leased_at = now(),
            lease_expires_at = now() + interval '5 minutes'
        where (f.crawl_id, f.url_hash) in (
            select crawl_id, url_hash from crawl_frontier
            where crawl_id = 'c0000000-0000-0000-0000-00000000000c'
              and state = 'pending'
            order by depth, url_hash limit 3
            for update skip locked)
        returning 1)
    select count(*) into claimed from lease;
    if claimed <> 3 then
        raise exception 'RECOVERY FAIL: worker-b claimed % of 3', claimed;
    end if;
    raise notice 'PASS  worker-b claimed all 3 without worker-a recovering';
end $$;

-- ---------------------------------------------------------------------------
-- 5. Only one active Google link per (website, service).
-- ---------------------------------------------------------------------------
insert into connections (id, organization_id, provider_key, external_id, label)
values ('9a000000-0000-0000-0000-00000000000a',
        '11111111-1111-1111-1111-111111111111','google','sub-123','a@example.com');
insert into connection_properties
  (id, organization_id, connection_id, provider_key, service, property_uri, matched_hosts)
values ('9e000000-0000-0000-0000-000000000001','11111111-1111-1111-1111-111111111111',
        '9a000000-0000-0000-0000-00000000000a','google','search_console',
        'sc-domain:example.com','{example.com}'),
       ('9e000000-0000-0000-0000-000000000002','11111111-1111-1111-1111-111111111111',
        '9a000000-0000-0000-0000-00000000000a','google','search_console',
        'https://www.example.com/','{www.example.com}');
insert into website_connections (organization_id, website_id, property_id, provider_key, service)
values ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
        '9e000000-0000-0000-0000-000000000001','google','search_console');

do $$
begin
    begin
        insert into website_connections (organization_id, website_id, property_id, provider_key, service)
        values ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
                '9e000000-0000-0000-0000-000000000002','google','search_console');
        raise exception 'LINK FAIL: a second active link was allowed';
    exception when unique_violation then
        raise notice 'PASS  second active search_console link rejected';
    end;
    -- but an unlinked historical row is still permitted
    insert into website_connections (organization_id, website_id, property_id, provider_key, service, status)
    values ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
            '9e000000-0000-0000-0000-000000000002','google','search_console','unlinked');
    raise notice 'PASS  historical unlinked row still permitted';
end $$;

-- ---------------------------------------------------------------------------
-- 6. An action cannot be marked applied without a before_state.
--    This is the invariant that makes every future write capability
--    (WordPress edits, Business Profile posts, Ads changes) revertible by
--    construction rather than by developer discipline.
-- ---------------------------------------------------------------------------
do $$
begin
    begin
        insert into actions (organization_id, website_id, capability, status, result)
        values ('11111111-1111-1111-1111-111111111111',
                '5117e000-0000-0000-0000-00000000000a',
                'page.meta.update','applied','succeeded');
        raise exception 'REVERT FAIL: an applied action with no before_state was allowed';
    exception when check_violation then
        raise notice 'PASS  applied action without before_state rejected';
    end;

    insert into actions (organization_id, website_id, capability, status, result,
                         before_state, after_state)
    values ('11111111-1111-1111-1111-111111111111',
            '5117e000-0000-0000-0000-00000000000a',
            'page.meta.update','applied','succeeded',
            '{"title": "About"}', '{"title": "About our church in London"}');
    raise notice 'PASS  applied action with before_state accepted';
end $$;

-- ---------------------------------------------------------------------------
-- 7. The scorecard is configuration, and disabled components are ABSENT from
--    the weighting rather than contributing zero. Authority is disabled until
--    a backlink vendor is contracted, so the enabled weights must still total 1.
-- ---------------------------------------------------------------------------
do $$
declare total numeric; disabled_count int;
begin
    select coalesce(sum(weight),0) into total
      from score_components where scoring_version = '1.0.0' and enabled;
    select count(*) into disabled_count
      from score_components where scoring_version = '1.0.0' and not enabled;

    if total <> 1.00 then
        raise exception 'SCORE FAIL: enabled weights total %, expected 1.00', total;
    end if;
    if disabled_count < 1 then
        raise exception 'SCORE FAIL: expected authority to be disabled pre-vendor';
    end if;
    raise notice 'PASS  enabled score weights total 1.00 with % component(s) awaiting a data source',
                 disabled_count;
end $$;

-- ---------------------------------------------------------------------------
-- 8. Only reversible capabilities may ever be granted standing approval.
--    Ads budget changes and redirects must never run unattended.
-- ---------------------------------------------------------------------------
do $$
declare bad text;
begin
    select string_agg(key, ', ') into bad
      from action_capabilities
     where allows_standing_approval and blast_radius <> 'reversible';
    if bad is not null then
        raise exception 'BLAST RADIUS FAIL: non-reversible capabilities allow standing approval: %', bad;
    end if;
    raise notice 'PASS  standing approval restricted to reversible capabilities';
end $$;

-- ---------------------------------------------------------------------------
-- 9. Every derived table carries provenance. A number on the dashboard must
--    always be traceable to the records and the calculation version that
--    produced it — this test is what stops that guarantee eroding one
--    convenient migration at a time.
-- ---------------------------------------------------------------------------
do $$
declare
    t text;
    required text[] := array['source','derived_from','computed_at'];
    col text;
    missing text := '';
begin
    foreach t in array array['issues','recommendations','external_metrics'] loop
        foreach col in array required loop
            if not exists (
                select 1 from information_schema.columns
                 where table_name = t and column_name = col
            ) then
                missing := missing || format('%s.%s ', t, col);
            end if;
        end loop;
    end loop;
    -- score_snapshots and plans carry scoring_version as their calculation
    -- version, so they are checked for the rest of the contract only.
    foreach t in array array['score_snapshots','plans'] loop
        foreach col in array array['source','derived_from','scoring_version'] loop
            if not exists (
                select 1 from information_schema.columns
                 where table_name = t and column_name = col
            ) then
                missing := missing || format('%s.%s ', t, col);
            end if;
        end loop;
    end loop;

    if missing <> '' then
        raise exception 'PROVENANCE FAIL: missing %', missing;
    end if;
    raise notice 'PASS  every derived table carries source, derived_from and a calculation version';
end $$;

-- ---------------------------------------------------------------------------
-- 10. The provenance index answers "where did this number come from" across
--     every derived table in one query.
-- ---------------------------------------------------------------------------
insert into score_snapshots
    (organization_id, website_id, as_of, scoring_version, total,
     technical_health, search_performance, content_health, analytics_coverage,
     components, source, derived_from, observed_from, observed_to)
values ('11111111-1111-1111-1111-111111111111','5117e000-0000-0000-0000-00000000000a',
        current_date, '1.0.0', 76, 85, 68, 73, 91,
        '{"technical_health":{"penalty":15,"issues_open":3}}',
        'derived',
        '{"crawl_id":"c0000000-0000-0000-0000-00000000000c","gsc_days":28}',
        current_date - 28, current_date - 3);

do $$
declare src text; ver text; frm jsonb;
begin
    select source, calculation_version, derived_from into src, ver, frm
      from provenance_index
     where table_name = 'score_snapshots'
       and website_id = '5117e000-0000-0000-0000-00000000000a';
    if src is null or ver <> '1.0.0' or frm->>'crawl_id' is null then
        raise exception 'PROVENANCE INDEX FAIL: source=%, version=%, from=%', src, ver, frm;
    end if;
    raise notice 'PASS  score of 76 traces to source=%, version=%, crawl=%',
                 src, ver, frm->>'crawl_id';
end $$;

-- ---------------------------------------------------------------------------
-- 11. Search Console access is not generic ownership. A property covers a URL
--     or it does not, and the difference between a domain property and a
--     URL-prefix property is exactly where a naive check goes wrong.
-- ---------------------------------------------------------------------------
do $$
declare
    cases text[][] := array[
        -- property_uri,                 url,                              expected
        array['sc-domain:example.com',   'https://www.example.com/about',  'true' ],
        array['sc-domain:example.com',   'http://blog.example.com/',       'true' ],
        array['sc-domain:example.com',   'https://example.com',            'true' ],
        array['sc-domain:example.com',   'https://notexample.com/',        'false'],
        array['sc-domain:example.com',   'https://example.com.evil.net/',  'false'],
        array['https://www.example.com/','https://www.example.com/about',  'true' ],
        array['https://www.example.com/','https://www.example.com',        'true' ],
        array['https://www.example.com/','http://www.example.com/about',   'false'],
        array['https://www.example.com/','https://blog.example.com/',      'false'],
        array['https://www.example.com/','https://example.com/',           'false'],
        array['https://example.com/shop/','https://example.com/about',     'false']
    ];
    i int;
    got boolean;
    want boolean;
begin
    for i in 1 .. array_length(cases, 1) loop
        got  := app.property_covers_url(cases[i][1], cases[i][2]);
        want := cases[i][3]::boolean;
        if got is distinct from want then
            raise exception 'COVERAGE FAIL: % vs % gave %, expected %',
                cases[i][1], cases[i][2], got, want;
        end if;
    end loop;
    raise notice 'PASS  property coverage correct across % cases (www, scheme, subdomain, suffix-spoof, path)',
                 array_length(cases, 1);
end $$;

-- ---------------------------------------------------------------------------
-- 12. A property the user can merely see is not ownership evidence, and a
--     covering property held as owner is.
-- ---------------------------------------------------------------------------
do $$
declare sufficient boolean; covers boolean;
begin
    select is_sufficient_evidence, covers_canonical_url into sufficient, covers
      from website_ownership_evidence
     where website_id = '5117e000-0000-0000-0000-00000000000a';

    -- The fixture links sc-domain:example.com with no permission_level set,
    -- so coverage holds but evidence does not.
    if covers is not true then
        raise exception 'EVIDENCE FAIL: expected sc-domain:example.com to cover the canonical URL';
    end if;
    if sufficient is not false then
        raise exception 'EVIDENCE FAIL: a property with no owner permission was accepted as evidence';
    end if;

    update connection_properties set permission_level = 'siteOwner'
     where id = '9e000000-0000-0000-0000-000000000001';

    select is_sufficient_evidence into sufficient
      from website_ownership_evidence
     where website_id = '5117e000-0000-0000-0000-00000000000a';
    if sufficient is not true then
        raise exception 'EVIDENCE FAIL: siteOwner on a covering property was rejected';
    end if;
    raise notice 'PASS  ownership requires a COVERING property held as owner, not mere access';
end $$;

-- ---------------------------------------------------------------------------
-- 13. gsc_totals_daily is a real table, not a view over the dimensional ones.
--     If a future migration "simplifies" it away, this fails loudly.
-- ---------------------------------------------------------------------------
do $$
begin
    if not exists (
        select 1 from information_schema.tables
         where table_name = 'gsc_totals_daily' and table_type = 'BASE TABLE'
    ) then
        raise exception 'RECONCILIATION FAIL: gsc_totals_daily must remain an independently fetched table';
    end if;
    raise notice 'PASS  reconciliation authority is stored, never derived from dimensional rows';
end $$;

-- ---------------------------------------------------------------------------
-- 14. Determinism is declared, and a modelled estimate cannot claim it.
--     "Same inputs, same version, same output" is true of a scoring function
--     and false of anything that calls a model. Both contracts are recorded;
--     neither is allowed to impersonate the other.
-- ---------------------------------------------------------------------------
insert into data_providers (key, label, capabilities, status)
values ('example_vendor','Example vendor','{keyword_volume}','evaluating');

do $$
begin
    begin
        insert into external_metrics
            (organization_id, website_id, subject_kind, subject_ref, metric,
             value_numeric, provider_key, source, is_modelled, deterministic, as_of)
        values ('11111111-1111-1111-1111-111111111111',
                '5117e000-0000-0000-0000-00000000000a',
                'keyword','church in london','search_volume',
                2400,'example_vendor','modelled', true, true, current_date);
        raise exception 'DETERMINISM FAIL: a modelled estimate was recorded as reproducible';
    exception when check_violation then
        raise notice 'PASS  a modelled estimate cannot be declared deterministic';
    end;

    insert into external_metrics
        (organization_id, website_id, subject_kind, subject_ref, metric,
         value_numeric, provider_key, source, is_modelled, deterministic, as_of)
    values ('11111111-1111-1111-1111-111111111111',
            '5117e000-0000-0000-0000-00000000000a',
            'keyword','church in london','search_volume',
            2400,'example_vendor','modelled', true, false, current_date);
    raise notice 'PASS  the same estimate is accepted once declared non-reproducible';
end $$;

-- ---------------------------------------------------------------------------
-- 15. Model output is accountable even though it is not reproducible: which
--     provider, which model version, which prompt version, grounded in which
--     evidence, generated when.
-- ---------------------------------------------------------------------------
do $$
declare prov text; ev jsonb;
begin
    begin
        insert into llm_calls (organization_id, website_id, purpose, model, status)
        values ('11111111-1111-1111-1111-111111111111',
                '5117e000-0000-0000-0000-00000000000a',
                'weekly_plan','some-model','ok');
        raise exception 'ACCOUNTABILITY FAIL: a generation with no provider or evidence was allowed';
    exception when check_violation then
        raise notice 'PASS  a generation without provider or grounding evidence is rejected';
    end;

    insert into llm_calls
        (organization_id, website_id, purpose, model, model_provider, model_version,
         prompt_version, derived_from, attached_to_table, attached_to_id, status)
    values ('11111111-1111-1111-1111-111111111111',
            '5117e000-0000-0000-0000-00000000000a',
            'weekly_plan','some-model','anthropic','2026-05-01','weekly_plan.v1',
            '{"issue_ids":["a1b2"],"gsc_window":"2026-08-24/2026-09-20"}',
            'plans','p-1','ok');

    select model_provider, derived_from into prov, ev
      from llm_calls
     where website_id = '5117e000-0000-0000-0000-00000000000a'
       and purpose = 'weekly_plan' and status = 'ok';
    if prov is null or ev->'issue_ids' is null then
        raise exception 'ACCOUNTABILITY FAIL: provider=%, evidence=%', prov, ev;
    end if;
    raise notice 'PASS  model output records provider, version, prompt and its grounding evidence';
end $$;

-- ---------------------------------------------------------------------------
-- 16. The provenance index says which contract each row is under, so "can this
--     be reproduced?" is answered by the data, not by knowing the table.
-- ---------------------------------------------------------------------------
do $$
declare repro int; not_repro int;
begin
    select count(*) filter (where deterministic),
           count(*) filter (where not deterministic)
      into repro, not_repro
      from provenance_index
     where website_id = '5117e000-0000-0000-0000-00000000000a';
    if repro < 1 or not_repro < 1 then
        raise exception 'INDEX FAIL: reproducible=%, non-reproducible=%', repro, not_repro;
    end if;
    raise notice 'PASS  provenance index separates % reproducible from % model-dependent row(s)',
                 repro, not_repro;
end $$;


-- ---------------------------------------------------------------------------
-- 17. A client cannot grant itself the right to crawl. crawl_allowed is
--     derived and unwritable by client roles, so "the API never sets this"
--     does not depend on every handler remembering.
-- ---------------------------------------------------------------------------
set role app_user;
set app.user_id = 'aaaaaaaa-0000-0000-0000-000000000001';
do $$
begin
    begin
        update websites set crawl_allowed = true
         where id = '5117e000-0000-0000-0000-00000000000a';
        raise exception 'DERIVED FAIL: a client role set crawl_allowed';
    exception when insufficient_privilege then
        raise notice 'PASS  a client role cannot write crawl_allowed';
    end;

    begin
        update organizations set max_pages_per_crawl = 1000000
         where id = '11111111-1111-1111-1111-111111111111';
        raise exception 'DERIVED FAIL: a client role raised its own page cap';
    exception when insufficient_privilege then
        raise notice 'PASS  a client role cannot raise its own plan limits';
    end;

    -- but ordinary fields on the same rows remain writable
    update websites set name = 'Example Church'
     where id = '5117e000-0000-0000-0000-00000000000a';
    raise notice 'PASS  ordinary columns on the same table stay writable';
end $$;
reset role;
reset app.user_id;


-- ---------------------------------------------------------------------------
-- 18. AI output is accountable or it is not stored. A generation that cannot
--     say which provider produced it and which rows it was grounded in is a
--     sentence with no origin, and the database refuses it rather than letting
--     the claim sit there looking true.
-- ---------------------------------------------------------------------------
reset role;
do $$
begin
    begin
        insert into llm_calls (organization_id, purpose, model, status,
                               derived_from)
        values ('11111111-1111-1111-1111-111111111111','weekly_plan','some-model',
                'ok', '{}'::jsonb);
        raise exception 'AI FAIL: an ungrounded generation was accepted';
    exception when check_violation then
        raise notice 'PASS  a generation with no provider or evidence is refused';
    end;

    -- A refusal is a record of a call that produced nothing usable, so it is
    -- exempt: there is no output to be accountable for.
    insert into llm_calls (organization_id, purpose, model, status, derived_from)
    values ('11111111-1111-1111-1111-111111111111','weekly_plan','some-model',
            'refused', '{}'::jsonb);
    raise notice 'PASS  a refusal is recorded without a grounding claim';
end $$;


-- ---------------------------------------------------------------------------
-- 19. The explanation cache is global ON PURPOSE, and its reachability is a
--     property worth asserting rather than assuming: the request path must be
--     able to read it (or the audit screen silently shows templates forever),
--     and it must carry no tenant column to scope it by.
-- ---------------------------------------------------------------------------
do $$
declare has_org boolean; rls boolean;
begin
    select exists(select 1 from information_schema.columns
                   where table_name = 'issue_explanations'
                     and column_name = 'organization_id'),
           (select relrowsecurity from pg_class where relname = 'issue_explanations')
      into has_org, rls;

    if has_org then
        raise exception 'CACHE FAIL: issue_explanations gained a tenant column; '
                        'either scope it properly or keep it global';
    end if;
    if rls then
        raise exception 'CACHE FAIL: row-level security on a table with no '
                        'policy reads as empty, not as an error';
    end if;
    raise notice 'PASS  the explanation cache is global and readable';
end $$;

set role app_user;
set app.user_id = 'aaaaaaaa-0000-0000-0000-000000000001';
do $$
declare n int;
begin
    select count(*) into n from issue_explanations;
    raise notice 'PASS  the request-path role can read the explanation cache (% rows)', n;
end $$;
reset role;
reset app.user_id;


-- ---------------------------------------------------------------------------
-- 20. A plan and its recommendations say whose words they are. A customer
--     reading templated advice while believing a model wrote it, or the
--     reverse, is a difference the row has to record.
-- ---------------------------------------------------------------------------
do $$
declare plan_id uuid;
begin
    insert into plans (organization_id, website_id, week_start, scoring_version,
                       prompt_version, model)
    values ('11111111-1111-1111-1111-111111111111',
            '5117e000-0000-0000-0000-00000000000a','2026-09-21','1.0.0',
            'weekly_plan.v1','template')
    on conflict (website_id, week_start) do update set model = excluded.model
    returning id into plan_id;

    begin
        update plans set fallback_reason = 'because' where id = plan_id;
        raise exception 'PLAN FAIL: an unrecognised fallback reason was accepted';
    exception when check_violation then
        raise notice 'PASS  a plan cannot claim an unrecognised fallback reason';
    end;

    begin
        insert into recommendations (organization_id, website_id, plan_id, kind,
                                     rank, title, impact_score, effort,
                                     confidence, prose_source)
        values ('11111111-1111-1111-1111-111111111111',
                '5117e000-0000-0000-0000-00000000000a', plan_id, 'fix_issue',
                1, 'Write titles', 10, 'low', 0.9, 'a-human');
        raise exception 'PLAN FAIL: an unrecognised prose source was accepted';
    exception when check_violation then
        raise notice 'PASS  recommendation prose names a source we recognise';
    end;

    delete from plans where id = plan_id;
end $$;


-- ---------------------------------------------------------------------------
-- 21. A conversation is tenant data like any other. The Strategist reads a
--     customer's whole dataset through typed tools, so the one thing that
--     must not be special about it is its storage.
-- ---------------------------------------------------------------------------
reset role;
insert into conversations (id, organization_id, website_id, title) values
  ('c0117e00-0000-0000-0000-00000000000a','11111111-1111-1111-1111-111111111111',
   '5117e000-0000-0000-0000-00000000000a','Why did my traffic fall?'),
  ('c0117e00-0000-0000-0000-00000000000b','22222222-2222-2222-2222-222222222222',
   '5117e000-0000-0000-0000-00000000000b','Their private question')
on conflict (id) do nothing;

insert into conversation_messages (organization_id, conversation_id, seq, role,
                                   content)
values ('22222222-2222-2222-2222-222222222222',
        'c0117e00-0000-0000-0000-00000000000b', 1, 'user',
        'Something commercially sensitive')
on conflict (conversation_id, seq) do nothing;

set role app_user;
set app.user_id = 'aaaaaaaa-0000-0000-0000-000000000001';
do $$
declare n int; t text;
begin
    select count(*), min(title) into n, t from conversations;
    if n <> 1 or t <> 'Why did my traffic fall?' then
        raise exception 'CHAT FAIL: org A sees % conversation(s): %', n, t;
    end if;
    raise notice 'PASS  a conversation is visible only to its own organisation';

    select count(*) into n from conversation_messages;
    if n <> 0 then
        raise exception 'CHAT FAIL: org A read % of org B''s messages', n;
    end if;
    raise notice 'PASS  another organisation''s chat messages are unreadable';
end $$;


-- ---------------------------------------------------------------------------
-- 22. The spend log is written by the system, never by a client role. A
--     browser-reachable role that could insert here could inflate its own
--     recorded spend and pollute the cost-per-feature figures that pricing
--     decisions come from. Reading its own remains allowed.
-- ---------------------------------------------------------------------------
do $$
begin
    begin
        insert into llm_calls (organization_id, purpose, model, model_provider,
                               cost_usd, derived_from)
        values ('11111111-1111-1111-1111-111111111111','strategist_chat','m',
                'anthropic', 0, '{"x":1}'::jsonb);
        raise exception 'METER FAIL: a client role wrote the spend log';
    exception when insufficient_privilege then
        raise notice 'PASS  a client role cannot write the spend log';
    end;

    perform count(*) from llm_calls;
    raise notice 'PASS  a client role can still read its own metering';
end $$;
reset role;
reset app.user_id;


-- ---------------------------------------------------------------------------
-- 23. The schedule's claim. Inserting the row IS winning the right to run the
--     slot, so the unique index has to be the thing that stops a job running
--     twice — not a comment, not a Redis key, not an advisory lock somebody
--     forgets to take.
-- ---------------------------------------------------------------------------
reset role;
do $$
declare first_id bigint; second_id bigint;
begin
    delete from scheduled_runs
     where website_id = '5117e000-0000-0000-0000-00000000000a';

    insert into scheduled_runs (organization_id, website_id, job, window_start)
    values ('11111111-1111-1111-1111-111111111111',
            '5117e000-0000-0000-0000-00000000000a', 'crawl_website',
            '2026-09-23 03:07:00+00')
    on conflict (website_id, job, window_start) do nothing
    returning id into first_id;

    insert into scheduled_runs (organization_id, website_id, job, window_start)
    values ('11111111-1111-1111-1111-111111111111',
            '5117e000-0000-0000-0000-00000000000a', 'crawl_website',
            '2026-09-23 03:07:00+00')
    on conflict (website_id, job, window_start) do nothing
    returning id into second_id;

    if first_id is null or second_id is not null then
        raise exception 'SCHEDULE FAIL: the slot was claimed twice (% and %)',
                        first_id, second_id;
    end if;
    raise notice 'PASS  a schedule slot can only be claimed once';

    -- Tomorrow is a different slot, and must be claimable.
    insert into scheduled_runs (organization_id, website_id, job, window_start)
    values ('11111111-1111-1111-1111-111111111111',
            '5117e000-0000-0000-0000-00000000000a', 'crawl_website',
            '2026-09-24 03:07:00+00')
    on conflict (website_id, job, window_start) do nothing
    returning id into second_id;
    if second_id is null then
        raise exception 'SCHEDULE FAIL: the next night could not be claimed';
    end if;
    raise notice 'PASS  the next night is a separate claim';
end $$;

-- Readable by its own organisation, written only by the system — the same
-- shape as llm_calls, and for the same reason: a customer may see that
-- Tuesday's sync failed; nothing reachable from a browser may write that.
set role app_user;
set app.user_id = 'aaaaaaaa-0000-0000-0000-000000000001';
do $$
declare n int;
begin
    select count(*) into n from scheduled_runs;
    if n < 2 then
        raise exception 'SCHEDULE FAIL: org A cannot read its own runs (%)', n;
    end if;

    begin
        insert into scheduled_runs (organization_id, website_id, job, window_start)
        values ('11111111-1111-1111-1111-111111111111',
                '5117e000-0000-0000-0000-00000000000a', 'crawl_website',
                '2030-01-01 03:00:00+00');
        raise exception 'SCHEDULE FAIL: a client role wrote the schedule log';
    exception when insufficient_privilege then
        raise notice 'PASS  a client role reads its schedule but cannot write it';
    end;
end $$;

set app.user_id = 'bbbbbbbb-0000-0000-0000-000000000002';
do $$
declare n int;
begin
    select count(*) into n from scheduled_runs;
    if n <> 0 then
        raise exception 'SCHEDULE FAIL: org B read % of org A''s runs', n;
    end if;
    raise notice 'PASS  another organisation''s schedule is invisible';
end $$;
reset role;
reset app.user_id;


-- ---------------------------------------------------------------------------
-- 24. A deletion record that the deletion deletes is not a record.
--
--     `deletion_receipts` sits outside the tenancy graph deliberately: no
--     foreign keys, nothing cascades into it, and it outlives everything it
--     describes. It is also operator-only — there is no organisation left to
--     scope a policy by, so the request-path role gets one that matches
--     nothing rather than a REVOKE that `roles.sql` would silently undo on
--     its next run.
-- ---------------------------------------------------------------------------
reset role;
do $$
declare receipt uuid; still int;
begin
    insert into deletion_receipts (user_id, organization_ids, rows_deleted)
    values ('aaaaaaaa-0000-0000-0000-000000000001',
            array['22222222-2222-2222-2222-222222222222'::uuid],
            '{"gsc_query_daily": 40321}')
    returning id into receipt;

    delete from organizations where id = '22222222-2222-2222-2222-222222222222';

    select count(*) into still from deletion_receipts where id = receipt;
    if still <> 1 then
        raise exception 'RECEIPT FAIL: the deletion deleted its own record';
    end if;
    raise notice 'PASS  a deletion receipt outlives the organisation it describes';

    delete from deletion_receipts where id = receipt;
end $$;

set role app_user;
set app.user_id = 'aaaaaaaa-0000-0000-0000-000000000001';
do $$
declare n int;
begin
    select count(*) into n from deletion_receipts;
    if n <> 0 then
        raise exception 'RECEIPT FAIL: a client role read % receipt(s)', n;
    end if;
    raise notice 'PASS  deletion receipts are invisible to the request path';
end $$;
reset role;
reset app.user_id;


-- ---------------------------------------------------------------------------
-- 25. The encrypted refresh token is the PARENT of the connection, so
--     deleting the connection leaves the secret behind. It is the one thing a
--     cascade actively cannot help with, and the reason account deletion goes
--     through the vault rather than trusting the foreign keys.
-- ---------------------------------------------------------------------------
do $$
declare token uuid; still int;
begin
    insert into secrets.oauth_tokens (ciphertext, wrapped_dek, nonce)
    values ('x','y','z') returning id into token;

    insert into connections (organization_id, provider_key, external_id, label,
                             refresh_token_id)
    values ('11111111-1111-1111-1111-111111111111','google','smoke-token',
            'owner@example.com', token);

    delete from connections where external_id = 'smoke-token';

    select count(*) into still from secrets.oauth_tokens where id = token;
    if still <> 1 then
        raise exception 'VAULT FAIL: the smoke test''s premise is wrong';
    end if;
    raise notice 'PASS  deleting a connection leaves its secret behind, as expected';

    delete from secrets.oauth_tokens where id = token;
end $$;
