-- 0004_crawl.sql
-- Crawl storage. The split that matters: `pages` is a STABLE IDENTITY per URL,
-- `page_snapshots` is an APPEND-ONLY observation per crawl. Without that split
-- there is no "what changed", which is the product.
--
-- Raw HTML never enters Postgres. It goes to object storage keyed by
--   {website_id}/{crawl_id}/{url_hash}.html.gz
-- and only the key is stored here. A 500-page website crawled weekly is otherwise
-- millions of rows of text nobody queries.

create table crawls (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id)  on delete cascade,
    website_id         uuid not null references websites(id) on delete cascade,
    status          text not null default 'queued'
                         check (status in ('queued','running','completed','failed','cancelled')),
    trigger         text not null
                         check (trigger in ('onboarding','manual','scheduled','verification')),
    -- For verification crawls: the fix this crawl exists to check.
    verifies_issue_id uuid,
    config          jsonb not null default '{}'::jsonb,
    robots_key      text,
    sitemap_urls    text[] not null default '{}',
    pages_discovered int not null default 0,
    pages_fetched    int not null default 0,
    pages_rendered   int not null default 0,
    fetch_errors     int not null default 0,
    error_summary    jsonb,
    queued_at       timestamptz not null default now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    -- Set when a worker dies mid-crawl; a supervisor re-leases the frontier.
    heartbeat_at    timestamptz
);
create index on crawls (website_id, queued_at desc);
create index on crawls (status) where status in ('queued','running');

-- The frontier IS the queue. Kept in Postgres rather than Redis so that a crawl
-- is resumable and idempotent by construction: workers lease rows with
-- SELECT ... FOR UPDATE SKIP LOCKED, and a killed worker's lease simply expires.
-- A crawl that dies at page 400 of 500 resumes at 400, never at 1.
--
-- RECOVERY INVARIANT:
--
--   A URL whose lease expires becomes eligible for another worker WITHOUT
--   requiring the original worker to recover.
--
-- That is the guarantee worth stating — "the crawler uses Postgres" is not.
-- It means worker death is a non-event: no supervisor has to detect it, no
-- peer has to hand off, and no in-flight URL is lost. A sweeper flips expired
-- leases back to 'pending' and any worker picks them up.
create table crawl_frontier (
    crawl_id        uuid  not null references crawls(id) on delete cascade,
    url_hash        bytea not null,
    url             text  not null,
    depth           int   not null default 0,
    discovered_from bytea,
    discovered_at   timestamptz not null default now(),
    state           text  not null default 'pending'
                          check (state in ('pending','leased','done','failed','skipped')),
    skip_reason     text,
    -- Lease bookkeeping. worker_id is diagnostic only: reclaiming an expired
    -- lease never consults it, because that would make recovery depend on
    -- knowing something about the dead worker.
    worker_id         text,
    leased_at         timestamptz,
    lease_expires_at  timestamptz,
    attempts          int   not null default 0,
    last_error        text,
    primary key (crawl_id, url_hash)
);
-- The lease query's index. Partial, so it stays small as the crawl drains.
create index on crawl_frontier (crawl_id, depth) where state = 'pending';
-- The sweeper's index: find expired leases regardless of which worker held them.
create index on crawl_frontier (lease_expires_at) where state = 'leased';

-- Stable per-URL identity across every crawl of a website.
create table pages (
    id              uuid primary key default gen_random_uuid(),
    organization_id          uuid  not null references organizations(id)  on delete cascade,
    website_id         uuid  not null references websites(id) on delete cascade,
    url             text  not null,
    url_hash        bytea not null,
    path            text  not null,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),
    last_status     int,
    last_crawl_id   uuid,
    is_indexable    boolean,
    -- Set when a crawl completes without encountering the URL.
    gone_at         timestamptz,
    unique (website_id, url_hash)
);
create index on pages (website_id) where gone_at is null;
create index on pages using gin (path gin_trgm_ops);

-- Append-only observation. Partitioned monthly by fetched_at.
create table page_snapshots (
    id              uuid not null default gen_random_uuid(),
    organization_id          uuid not null,
    website_id         uuid not null,
    page_id         uuid not null,
    crawl_id        uuid not null,
    fetched_at      timestamptz not null,

    status_code     int,
    content_type    text,
    render_mode     text not null default 'http'
                         check (render_mode in ('http','browser')),
    response_time_ms int,
    bytes           int,
    redirect_chain  jsonb,
    fetch_error     text,

    -- Deduplication and change detection. Equal content_hash between crawls
    -- means nothing changed and the LLM explanation can be reused from cache.
    content_hash    bytea,
    raw_key         text,

    title           text,
    title_len       int,
    meta_description text,
    meta_description_len int,
    h1              text[],
    heading_counts  jsonb,
    word_count      int,
    text_hash       bytea,
    lang            text,
    canonical_url   text,
    canonical_is_self boolean,
    robots_meta     text[],
    x_robots_tag    text[],
    hreflang        jsonb,
    viewport_present boolean,
    schema_types    text[],
    schema_errors   jsonb,
    open_graph      jsonb,

    depth           int,
    internal_inlinks  int,
    internal_outlinks int,
    external_outlinks int,
    images_total      int,
    images_missing_alt int,
    images_oversized_bytes int,

    -- Anything an extractor adds later without a migration.
    extraction      jsonb not null default '{}'::jsonb,

    primary key (id, fetched_at)
) partition by range (fetched_at);

create index on page_snapshots (page_id, fetched_at desc);
create index on page_snapshots (crawl_id);
create index on page_snapshots (website_id, fetched_at desc);

-- Internal link graph, per crawl. Needed for the internal-linking
-- recommendation and for orphan-page detection. External links are stored only
-- where they are broken or nofollow-relevant, to keep the table bounded.
create table page_links (
    crawl_id      uuid  not null references crawls(id) on delete cascade,
    website_id       uuid  not null,
    from_page_id  uuid  not null,
    to_url_hash   bytea not null,
    to_url        text  not null,
    to_page_id    uuid,
    anchor_text   text,
    rel           text[],
    is_internal   boolean not null,
    status_code   int,               -- filled by the link checker pass
    primary key (crawl_id, from_page_id, to_url_hash)
);
create index on page_links (crawl_id, to_page_id) where is_internal;
create index on page_links (crawl_id) where status_code >= 400;

-- Field performance, sampled. Never run this per page: pick template
-- representatives (home, one article, one category, one contact) per crawl.
create table psi_samples (
    id            uuid primary key default gen_random_uuid(),
    organization_id        uuid not null,
    website_id       uuid not null references websites(id) on delete cascade,
    crawl_id      uuid references crawls(id) on delete set null,
    page_id       uuid,
    url           text not null,
    strategy      text not null default 'mobile' check (strategy in ('mobile','desktop')),
    source        text not null default 'crux_field'
                       check (source in ('crux_field','lighthouse_lab')),
    lcp_ms        int,
    inp_ms        int,
    cls           numeric(6,3),
    ttfb_ms       int,
    performance_score numeric(5,2),
    raw_key       text,
    collected_at  timestamptz not null default now()
);
create index on psi_samples (website_id, collected_at desc);
