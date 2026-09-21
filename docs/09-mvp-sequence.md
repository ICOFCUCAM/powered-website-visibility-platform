# 09 — MVP build sequence

Ordered so that each milestone is demonstrable and the riskiest external
dependency starts first. Each has an acceptance test that a person can run.

## M0 — Day one, in parallel with everything

**Google Cloud project, OAuth client, privacy policy, verification submitted.**

Not code. It is first because it is the longest-lead item in the project:
sensitive-scope verification takes weeks and bounces at least once. Build
against test users while it is in review.

*Done when:* consent screen configured, verification submitted, 100-test-user
cap understood and acceptable for the alpha.

## M1 — Foundations

Migrations `0001`–`0006` applied. Auth, orgs, membership, site creation.
FastAPI skeleton with the dependency chain that binds org scope. Next.js shell
with sign-in.

*Done when:* two users in two orgs cannot see each other's sites, proven by a
test that queries as each and asserts empty.

## M2 — Google Hub, end to end

OAuth with PKCE, token vault, discovery across GSC and GA4, host matching,
link creation, 16-month backfill, nightly incremental, sync bookkeeping. The
Hub screen and the first three wizard steps.

*Done when:* a real user connects a real Google account and, within ten
minutes, the API returns 16 months of daily clicks and impressions that
**reconcile with what they see in the Search Console UI** — including the
anonymised-clicks gap being displayed rather than hidden.

This is the milestone that proves the product's premise. Everything after it
is comparatively conventional.

## M3 — Crawler

robots and sitemap seeding, Postgres frontier, fetch workers with host rate
limiting, render escalation, extraction into `page_snapshots`, link graph, raw
storage, crawl progress API, wizard step 4.

*Done when:* a 500-page site crawls within 20 minutes at one request per second
per host; killing a worker mid-crawl loses no progress; re-crawling an
unchanged site produces identical `content_hash` values throughout.

## M4 — Analysis and scoring

The issue catalogue as deterministic rules. Fingerprints, `issues` and
`issue_observations`. Per-site expected-CTR curve. `scoring.py` under
`SCORING_VERSION = "1.0.0"`. Crawl-to-crawl diffing.

*Done when:* two consecutive crawls of a site where one title was removed
produce exactly one new issue, with the correct fingerprint, and restoring the
title flips it to `resolved` — not a new row.

## M5 — AI layer

Issue explanations with the evidence cache. Weekly plan generation with the
`last_week` join. Keyword expansion ranked on the site's own GSC impressions.
Output validation, budget admission, `llm_calls` metering.

*Done when:* the numbers validator rejects a deliberately-poisoned generation;
a 500-page crawl costs under $0.20 in explanations after cache warm-up; and a
regenerated plan on unchanged data produces the same four priorities in the
same order.

## M6 — The screens

Home (single `/overview` call), Google Hub, SEO with its four tabs, issue
detail with `[Mark as fixed]` and verification, settings. Every empty and
partial state written.

*Done when:* a new user completes the wizard and lands on a Home screen with a
populated 28-day chart and at least one actionable recommendation, without
seeing a single "no data yet" panel.

## M7 — The loop closes

`mark-applied` → `fix_actions` → verification crawl → `verified` / `regressed`,
surfaced in Recent Changes and in the next weekly plan.

*Done when:* fixing a missing title and clicking "Mark as fixed" shows
"verified" within five minutes, and the following week's plan opens by
referencing it.

## M8 — Weekly report

HTML email with score, deltas, the four priorities, what changed, what was
verified. Scheduled per site in the site's timezone. Unsubscribe, delivery
logging, a plain-text alternative.

*Done when:* a report renders correctly in Gmail, Outlook and Apple Mail, and
the figures in it match the dashboard for the same window exactly.

## M9 — Launch readiness

Plan limits enforced at admission. Stripe checkout. Partition scheduler
verified three months ahead. Backups with a **tested restore**. Error tracking,
crawl-failure and sync-failure alerting. `/bot` page live. Terms, privacy and a
data-deletion path that genuinely deletes.

*Done when:* a restore from backup into a scratch environment is performed
successfully and timed.

---

## Sequencing notes

**The Strategist is not in the MVP.** It is the most impressive screen and the
least essential: it answers questions about data the other milestones produce.
Build it first and there is nothing to ask about. It slots in immediately after
M8, when the data layer is proven, and it will take about a week.

**AI visibility ships as proxies only.** The five computable rules in M4 are
honest and free. Live measurement is a v3 project with its own cost model.

**Competitors and backlinks are not in the MVP at all** and the screen says so.
They require a licensed data source; get quotes during M2 so the v2 pricing is
grounded in real COGS.

**What could still go wrong, in rough order of likelihood:** OAuth verification
slipping past M6 (mitigated by starting at M0 and staying inside the test-user
cap); GSC quota during multi-tenant backfill (mitigated by the low-priority
pool and `quota_hits` instrumentation); render-worker memory (mitigated by pool
isolation and hard limits); and AI cost per crawl (mitigated by the evidence
cache, and measurable from day one through `llm_calls`).
