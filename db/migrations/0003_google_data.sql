-- 0003_google_data.sql
-- Normalised Google performance data. This is the Hub's output contract: the
-- analysis engines read these tables and never call a Google API themselves.
--
-- Two facts drive the whole design:
--
--  1. Google ANONYMISES low-volume queries. Rows returned with a `query`
--     dimension therefore do NOT sum to the website's real totals — commonly
--     30-50% of clicks are missing. So the unsliced daily total is fetched and
--     stored SEPARATELY, and the UI reports the gap as anonymised rather than
--     letting the user discover the discrepancy.
--
--  2. `position` is an average, weighted by impressions within the row's
--     bucket. Aggregating it across days or queries with avg() is wrong.
--     Always sum(position*impressions)/sum(impressions). See the views below.

-- ---------------------------------------------------------------------------
-- Search Console
-- ---------------------------------------------------------------------------

-- RECONCILIATION AUTHORITY. The only sanctioned source of site-level totals.
-- Fetched unsliced, so it matches the Search Console UI exactly.
create table gsc_totals_daily (
    organization_id      uuid not null,
    website_id     uuid not null references websites(id) on delete cascade,
    date        date not null,
    clicks      int     not null,
    impressions int     not null,
    position    numeric(6,2),
    primary key (website_id, date)
);

-- ANALYTICAL SUBSET. Query-level rows, partitioned monthly: the largest table
-- in the system for most tenants. Never sum this for a site total.
create table gsc_query_daily (
    organization_id      uuid not null,
    website_id     uuid not null,
    date        date not null,
    query_hash  bytea not null,           -- sha256(lower(query))
    query       text  not null,
    country     text  not null default 'ZZZ',
    device      text  not null default 'ALL',
    clicks      int     not null,
    impressions int     not null,
    position    numeric(6,2) not null,
    primary key (website_id, date, query_hash, country, device)
) partition by range (date);

create index on gsc_query_daily (website_id, query_hash, date);
create index on gsc_query_daily (website_id, date) include (clicks, impressions);

-- ANALYTICAL SUBSET. Page-level rows. Never sum this for a site total.
create table gsc_page_daily (
    organization_id      uuid not null,
    website_id     uuid not null,
    date        date not null,
    url_hash    bytea not null,           -- sha256(normalised url)
    url         text  not null,
    page_id     uuid,                     -- resolved against pages(id) when known
    country     text  not null default 'ZZZ',
    device      text  not null default 'ALL',
    clicks      int     not null,
    impressions int     not null,
    position    numeric(6,2) not null,
    primary key (website_id, date, url_hash, country, device)
) partition by range (date);

create index on gsc_page_daily (website_id, url_hash, date);

-- Query x page, needed to answer "which page ranks for this term" and to build
-- the CTR-opportunity finding. Expensive: fetched only for the website's top N
-- queries, and only for the trailing 90 days.
create table gsc_query_page_daily (
    organization_id      uuid not null,
    website_id     uuid not null,
    date        date not null,
    query_hash  bytea not null,
    query       text  not null,
    url_hash    bytea not null,
    url         text  not null,
    clicks      int     not null,
    impressions int     not null,
    position    numeric(6,2) not null,
    primary key (website_id, date, query_hash, url_hash)
) partition by range (date);

-- ---------------------------------------------------------------------------
-- Analytics (GA4)
-- ---------------------------------------------------------------------------

create table ga4_daily (
    organization_id          uuid not null,
    website_id         uuid not null references websites(id) on delete cascade,
    date            date not null,
    channel_group   text not null default 'ALL',
    sessions        int  not null default 0,
    active_users    int  not null default 0,
    engaged_sessions int not null default 0,
    engagement_rate numeric(6,4),
    avg_engagement_seconds numeric(8,2),
    key_events      int  not null default 0,
    primary key (website_id, date, channel_group)
);

create table ga4_page_daily (
    organization_id          uuid not null,
    website_id         uuid not null,
    date            date not null,
    url_hash        bytea not null,
    page_path       text  not null,
    page_id         uuid,
    sessions        int  not null default 0,
    active_users    int  not null default 0,
    engaged_sessions int not null default 0,
    avg_engagement_seconds numeric(8,2),
    key_events      int  not null default 0,
    primary key (website_id, date, url_hash)
) partition by range (date);

-- Outcome counts per mapped goal event.
create table ga4_goal_daily (
    organization_id      uuid not null,
    website_id     uuid not null,
    date        date not null,
    event_name  text not null,
    -- Sentinel rather than NULL: a primary key cannot contain an expression,
    -- and website-wide rows still need to be distinguishable from per-page rows.
    url_hash    bytea not null default '\x00'::bytea,
    event_count int not null default 0,
    primary key (website_id, date, event_name, url_hash)
) partition by range (date);

-- ---------------------------------------------------------------------------
-- Correct aggregation helpers. Use these; never avg(position).
-- ---------------------------------------------------------------------------

create view gsc_query_rollup as
select
    website_id,
    query_hash,
    min(query)                                      as query,
    min(date)                                       as first_date,
    max(date)                                       as last_date,
    sum(clicks)                                     as clicks,
    sum(impressions)                                as impressions,
    case when sum(impressions) > 0
         then sum(clicks)::numeric / sum(impressions) end            as ctr,
    case when sum(impressions) > 0
         then sum(position * impressions) / sum(impressions) end     as position
from gsc_query_daily
group by website_id, query_hash;

create view gsc_page_rollup as
select
    website_id,
    url_hash,
    min(url)                                        as url,
    sum(clicks)                                     as clicks,
    sum(impressions)                                as impressions,
    case when sum(impressions) > 0
         then sum(clicks)::numeric / sum(impressions) end            as ctr,
    case when sum(impressions) > 0
         then sum(position * impressions) / sum(impressions) end     as position
from gsc_page_daily
group by website_id, url_hash;

-- Share of clicks Google withheld as anonymised, per day. Surfaced in the UI so
-- the numbers are explained rather than merely inconsistent.
create view gsc_anonymised_share as
select
    t.website_id,
    t.date,
    t.clicks                             as total_clicks,
    coalesce(q.clicks, 0)                as attributed_clicks,
    t.clicks - coalesce(q.clicks, 0)     as anonymised_clicks,
    case when t.clicks > 0
         then (t.clicks - coalesce(q.clicks, 0))::numeric / t.clicks end
                                         as anonymised_share
from gsc_totals_daily t
left join (
    select website_id, date, sum(clicks) as clicks
    from gsc_query_daily
    where country = 'ZZZ' and device = 'ALL'
    group by website_id, date
) q on q.website_id = t.website_id and q.date = t.date;
