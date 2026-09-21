-- 0008_v1_alignment.sql
-- Gaps closed against the V1 specification.

-- ---------------------------------------------------------------------------
-- GA4 dimensional reporting (V1 spec s11, s28)
--
-- The spec's metric list includes country, device category, traffic source and
-- medium, which the typed daily tables did not cover. GA4's Data API returns a
-- different row set per dimension combination, so these arrive as separate
-- report requests and land in one long table keyed by dimension type.
--
-- Typed tables are kept for the two high-traffic shapes (site totals, per
-- page); this table absorbs the long tail without a migration per dimension.
-- ---------------------------------------------------------------------------
create table ga4_dimension_daily (
    organization_id uuid not null,
    website_id      uuid not null,
    date            date not null,
    dimension_type  text not null
        check (dimension_type in ('country','device_category','session_source',
                                  'session_medium','session_default_channel_group',
                                  'landing_page')),
    dimension_value text not null,
    sessions        int not null default 0,
    active_users    int not null default 0,
    engaged_sessions int not null default 0,
    engagement_rate numeric(6,4),
    page_views      int not null default 0,
    key_events      int not null default 0,
    primary key (website_id, date, dimension_type, dimension_value)
) partition by range (date);

create index on ga4_dimension_daily (website_id, dimension_type, date);

alter table ga4_dimension_daily enable row level security;
alter table ga4_dimension_daily force row level security;
create policy ga4_dimension_daily_read on ga4_dimension_daily for select
    using (app.is_org_member(organization_id));
create policy ga4_dimension_daily_write on ga4_dimension_daily for all
    using (app.can_write_org(organization_id))
    with check (app.can_write_org(organization_id));

-- Keep the partition helper aware of the new table.
create or replace function app.ensure_partitions_ahead(months int default 3)
returns void language plpgsql as $$
declare
    p text; i int;
    base date := date_trunc('month', current_date)::date;
begin
    foreach p in array array[
        'gsc_query_daily','gsc_page_daily','gsc_query_page_daily',
        'ga4_page_daily','ga4_goal_daily','ga4_dimension_daily','page_snapshots'
    ] loop
        for i in -1 .. months loop
            perform app.ensure_monthly_partition(p, (base + (i || ' month')::interval)::date);
        end loop;
    end loop;
end $$;
select app.ensure_partitions_ahead(3);

-- ---------------------------------------------------------------------------
-- The V1 spec's `pages` table (s29) as a view.
--
-- The spec models one mutable row per page. This schema keeps `pages` as a
-- stable identity and `page_snapshots` as append-only observations, because
-- "what changed since last week" is the product and an in-place update erases
-- it. This view gives the spec's exact shape — current state per page — over
-- that history, so application code can read it as specified while the
-- underlying facts stay intact.
-- ---------------------------------------------------------------------------
create view page_current as
select distinct on (p.id)
    p.id,
    p.organization_id,
    p.website_id,
    p.url,
    s.status_code,
    s.content_type,
    s.title,
    s.meta_description,
    s.canonical_url,
    s.h1,
    s.word_count,
    s.lang                as language,
    s.robots_meta,
    s.internal_outlinks   as internal_links,
    s.external_outlinks   as external_links,
    s.images_total        as image_count,
    case when s.images_total > 0
         then 1 - (s.images_missing_alt::numeric / s.images_total) end
                          as image_alt_coverage,
    s.response_time_ms    as load_time_ms,
    s.content_hash,
    s.fetched_at          as last_crawled_at
from pages p
join page_snapshots s on s.page_id = p.id
where p.gone_at is null
order by p.id, s.fetched_at desc;

-- ---------------------------------------------------------------------------
-- Audit issue resolution (V1 spec s20: [Mark resolved]).
-- `issues.status` already carries the lifecycle; this records who resolved an
-- issue by hand versus what a verification crawl proved.
-- ---------------------------------------------------------------------------
alter table issues add column resolved_by uuid references users(id) on delete set null;
alter table issues add column resolution_source text
    check (resolution_source in ('user_marked','verified_by_crawl','disappeared'));
