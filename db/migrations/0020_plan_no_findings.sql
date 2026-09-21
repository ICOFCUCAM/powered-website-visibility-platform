-- 0020_plan_no_findings.sql
--
-- `no_findings` was missing from the fallback vocabulary, so a website with
-- nothing wrong with it could not have a weekly plan written for it at all:
-- the insert failed the check constraint and the nightly job died.
--
-- It went unnoticed because every test of the plan seeded issues first — the
-- one shape nobody writes a fixture for is the healthy customer. Found by
-- running the scheduler against a database of real websites, 26 of which had
-- a clean audit.
--
-- A plan that says "nothing needs doing" is a legitimate weekly output, and
-- distinct from one that is templated because no model was configured. Both
-- deserve a name.

alter table plans drop constraint if exists plans_fallback_reason_check;

alter table plans add constraint plans_fallback_reason_check
    check (fallback_reason is null or fallback_reason in (
        'no_provider',
        'budget_exhausted',
        'validation_failed',
        'provider_error',
        'unsupported_numbers',
        'reordered_priorities',
        -- Nothing was open to write about. Not a degradation: the good case.
        'no_findings'));
