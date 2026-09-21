-- 0010_ownership.sql
--
-- Two DIFFERENT things, deliberately not one column:
--
--   ownership_verified   we have evidence this customer controls this website
--                        → required before privileged analysis, before raising
--                          the free page cap, before any write capability
--
--   crawl_allowed        derived from verification AND the customer's own
--                        settings (schedule off, plan suspended, robots
--                        blocking, an explicit pause)
--
-- Collapsing them means an unverified website inherits crawl rights from a
-- setting, or a verified customer cannot pause their own crawl. They move for
-- different reasons and are checked at different moments.

alter table websites
    add column ownership_verified_at timestamptz,
    add column ownership_method text
        check (ownership_method in ('search_console','dns_txt','file_token','manual_review')),
    -- Which specific property was the evidence, so a later permission change
    -- or property deletion can invalidate the claim it supported.
    add column ownership_property_id uuid references connection_properties(id) on delete set null,
    add column crawl_allowed boolean not null default false,
    add column crawl_blocked_reason text;

comment on column websites.crawl_allowed is
    'Derived, never set by hand: ownership_verified_at is not null AND the customer has not paused, AND the plan permits, AND robots does not block.';

-- ---------------------------------------------------------------------------
-- Does a Search Console property actually cover this URL?
--
-- Search Console access is NOT generic ownership of every URL a user types.
-- The property types differ in exactly the ways that matter:
--
--   sc-domain:example.com      every subdomain, every scheme, every port
--   https://www.example.com/   that scheme, that host, that path prefix only
--
-- So a user holding `https://www.example.com/` has proven nothing about
-- `https://blog.example.com/`, `http://www.example.com/` or
-- `https://example.com/`. Verification must check coverage, not mere access.
-- ---------------------------------------------------------------------------
create or replace function app.property_covers_url(property_uri text, url text)
returns boolean language plpgsql immutable as $$
declare
    domain text;
    host   text;
    prefix text;
    target text;
begin
    if property_uri is null or url is null then
        return false;
    end if;

    if property_uri like 'sc-domain:%' then
        -- Domain property: the host must be the domain or a subdomain of it.
        -- Scheme, port and path are all irrelevant.
        domain := lower(split_part(property_uri, ':', 2));
        host   := lower(substring(url from '^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:?#]+)'));
        if host is null or domain = '' then
            return false;
        end if;
        return host = domain or host like ('%.' || domain);
    end if;

    -- URL-prefix property: an exact scheme + host + path-prefix match.
    prefix := lower(property_uri);
    if right(prefix, 1) <> '/' then
        prefix := prefix || '/';
    end if;
    target := lower(url);
    -- A bare origin with no path is equivalent to that origin's root.
    if target ~ '^[a-z][a-z0-9+.-]*://[^/]+$' then
        target := target || '/';
    end if;
    return starts_with(target, prefix);
end $$;

comment on function app.property_covers_url(text, text) is
    'True when a Search Console property genuinely covers a URL. Domain properties cover subdomains and any scheme; URL-prefix properties do not.';

-- ---------------------------------------------------------------------------
-- A website is verified via Search Console only when the linked property
-- covers its canonical URL. This view is what the verification step reads, so
-- the check cannot be skipped by a handler that forgot it.
-- ---------------------------------------------------------------------------
create view website_ownership_evidence as
select
    w.id            as website_id,
    w.organization_id,
    w.canonical_url,
    cp.id           as property_id,
    cp.property_uri,
    cp.permission_level,
    app.property_covers_url(cp.property_uri, w.canonical_url) as covers_canonical_url,
    -- siteUnverifiedUser can see a property listed without having any data
    -- rights to it, so it can never constitute ownership evidence.
    --
    -- coalesce is load-bearing: an unknown permission level must read as
    -- INSUFFICIENT, not as NULL. A three-valued answer to "may we treat this
    -- as proof of ownership" is a security question answered with a shrug.
    coalesce(
        cp.permission_level in ('siteOwner','siteFullUser')
        and app.property_covers_url(cp.property_uri, w.canonical_url),
        false
    ) as is_sufficient_evidence
from websites w
join website_connections wc
      on wc.website_id = w.id and wc.service = 'search_console' and wc.status = 'active'
join connection_properties cp on cp.id = wc.property_id;
