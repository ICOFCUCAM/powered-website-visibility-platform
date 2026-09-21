-- 0015_issue_catalogue.sql
--
-- The issue catalogue is seeded from api/analysis/catalogue.py rather than
-- from SQL, so a rule and its metadata row cannot drift apart — registering a
-- rule whose catalogue entry is missing fails at import.
--
-- This migration only ensures the constraint that makes the seed safe to run
-- repeatedly, and the index the audit screen reads.

create index if not exists issues_website_status_impact
    on issues (website_id, status, impact_score desc);

create index if not exists issue_observations_website_observed
    on issue_observations (website_id, observed_at desc);

comment on table issue_types is
    'Seeded from api/analysis/catalogue.py on deploy. Do not edit by hand: a row
     that disagrees with its rule produces issues with the wrong weight.';
