-- 0016_observation_tenancy.sql
--
-- `issue_observations` had row-level security ENABLED with NO POLICY, which
-- means the request-path role reads nothing from it — silently, because a
-- SELECT filtered by RLS returns an empty set rather than an error. The audit
-- screen's history was therefore always empty and nothing failed.
--
-- The underlying cause is that this table was the one tenant table without an
-- organization_id, so it could not carry the standard one-predicate policy.
-- Adding the column restores the rule the whole schema depends on: every
-- tenant table carries org_id directly, and no policy is a join chain.
--
-- The same omission applied to page_links, llm_calls, backlink_changes,
-- alert_events and audit_log. Those are read by the service role today, so
-- nothing was visibly broken — which is exactly why they are worth fixing
-- before something starts reading them from the request path.

alter table issue_observations
    add column if not exists organization_id uuid;

update issue_observations o
   set organization_id = i.organization_id
  from issues i
 where i.id = o.issue_id and o.organization_id is null;

-- Left nullable deliberately: a NOT NULL would fail on any row whose issue was
-- deleted, and losing observation history to a constraint is a worse outcome
-- than a handful of orphan rows the policy simply never returns.
create index if not exists issue_observations_organization
    on issue_observations (organization_id, observed_at desc);

create policy issue_observations_read on issue_observations for select
    using (organization_id is not null and app.is_org_member(organization_id));
create policy issue_observations_write on issue_observations for all
    using (organization_id is not null and app.can_write_org(organization_id))
    with check (organization_id is not null and app.can_write_org(organization_id));

-- page_links is read by the audit's internal-linking findings.
alter table page_links add column if not exists organization_id uuid;
update page_links l
   set organization_id = w.organization_id
  from websites w
 where w.id = l.website_id and l.organization_id is null;

create policy page_links_read on page_links for select
    using (organization_id is not null and app.is_org_member(organization_id));
create policy page_links_write on page_links for all
    using (organization_id is not null and app.can_write_org(organization_id))
    with check (organization_id is not null and app.can_write_org(organization_id));

-- The same shape, found by the test that now guards against it: RLS enabled
-- with no policy on tables that DO carry organization_id. Nothing reads them
-- from the request path today, so nothing was visibly broken — which is
-- precisely why they are worth closing before something does.
--
-- Read-only on purpose. These are append-only records of what the system did:
-- a customer may inspect their own, and only the service role writes them.
create policy audit_log_read on audit_log for select
    using (app.is_org_member(organization_id));

create policy llm_calls_read on llm_calls for select
    using (organization_id is not null and app.is_org_member(organization_id));

create policy alert_events_read on alert_events for select
    using (app.is_org_member(organization_id));

create policy backlink_changes_read on backlink_changes for select
    using (app.is_org_member(organization_id));
