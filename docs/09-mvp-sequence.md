# 09 — MVP build sequence

> **Frozen — architectural source of truth.**
> Changes to this document are architectural decisions, not edits. Amend it
> deliberately, with the reason recorded in
> [10-decisions.md](10-decisions.md).

Ordered so that each milestone is demonstrable, the longest-lead external
dependency starts first, and the AI arrives as an interpretation layer over
accumulated evidence rather than a feature looking for something to say.

```
M0  Google OAuth + compliance
M1  Foundation + Supabase
M2  Google Hub
M3  Search Console
M4  GA4
M5  Website crawler
M6  SEO analysis
M7  Unified dashboard
M8  Weekly report
M9  AI Strategist
```

---

## M0 — Google OAuth and compliance

Not code, and not only the verification submission. The consent experience is
**designed and frozen here**, because Google reviews the whole story and
because rewriting it later means re-review.

Freeze in M0:

- [ ] OAuth consent-screen copy, app name, support email, logo
- [ ] The exact scope list, and a written justification for each one
- [ ] Privacy policy, published on the verified domain that owns the client
- [ ] Terms of Service
- [ ] Google API Services User Data Policy disclosure, including the Limited
      Use statement
- [ ] Data retention and deletion policy, matching what the code will do
- [ ] Disconnect and revocation behaviour: what is revoked, what is purged,
      what is retained, and what the user is told
- [ ] Test-user procedure while capped at 100 users
- [ ] A demo account for Google's reviewers, with data in it
- [ ] Screen recording of the full OAuth flow
- [ ] Screenshots of every screen that displays Google data

The story the review must see, in one line:

> Connect Google → choose your property → we read your data → we analyse it →
> you can disconnect at any time.

No unnecessary scopes. `webmasters.readonly` and `analytics.readonly` only;
`business.manage` is not requested until phase 3, and asking for it early would
widen the review surface for a feature that does not exist yet.

**Also frozen in M0: what "verified" means.** Not the Google review — our own
definition, because the policy documents above promise things about it:

- `ownership_verified` and `crawl_allowed` are separate, and the second is
  derived from the first plus the customer's own settings.
- Search Console access alone is not ownership. The linked property must
  *cover* the canonical URL — `app.property_covers_url()` — and be held as
  `siteOwner` or `siteFullUser`. A domain property covers subdomains and any
  scheme; a URL-prefix property covers neither.
- DNS TXT or a file token is the fallback where no covering property exists.

*Done when:* verification is submitted, every artefact above exists in the
repository rather than in someone's head, and the ownership rules are
implemented with tests rather than described.

## M1 — Foundation and Supabase

Migrations `0001`–`0007` applied. Supabase project provisioned with Auth,
Postgres and Storage. FastAPI skeleton with the dependency chain that binds org
scope. Next.js shell with sign-in.

Portability is a constraint from the first commit: Supabase-specific logic
lives in an adapter layer (`api/adapters/`), never in domain services. The
domain layer sees a repository interface and plain SQL. Auth is consumed as
"verify this JWT, give me a user id", not as a Supabase SDK sprinkled through
handlers.

*Done when:* two users in two organizations cannot see each other's websites, proven by a
test that queries as each and asserts empty — and `grep -r supabase api/domain/`
returns nothing.

## M2 — Google Hub

The bounded module: OAuth with PKCE, token vault, connection and resource
discovery, host matching, link creation, status and re-auth handling, the Hub
screen, and wizard steps 1–3.

No Search Console *data* yet. This milestone proves the connection layer alone,
which is what makes the Hub extractable later.

*Done when:* a real user connects a real Google account and the Hub screen
shows their discovered properties with permission levels, correctly auto-matched
to the website — and the import-boundary check in CI passes.

## M3 — Search Console

Backfill of 16 months, nightly incremental with a trailing re-fetch window,
quota handling, sync bookkeeping, and the correct-aggregation views.

*Done when:* the API returns 16 months of daily clicks and impressions that
**reconcile with what the user sees in the Search Console UI**, including the
anonymised-clicks gap being displayed rather than hidden.

This is the milestone that proves the product's premise.

## M4 — GA4

Property discovery, the goal-event mapping step, daily and page-level sync.

*Done when:* a user maps `contact_form_submit` as their goal and sees outcomes
per landing page — and a user who maps nothing sees "outcomes not configured"
rather than a fabricated conversion rate.

## M5 — Website crawler

robots and sitemap seeding, Postgres frontier, fetch workers with host rate
limiting, render escalation, extraction into `page_snapshots` plus the
versioned extraction document, link graph, raw storage, progress API, wizard
step 4.

*Done when:* a 500-page website crawls within 20 minutes at one request per second
per host; killing a worker mid-crawl loses no progress; and re-crawling an
unchanged website produces identical `content_hash` values throughout.

## M6 — SEO analysis

The issue catalogue as deterministic rules. Fingerprints, `issues` and
`issue_observations`. Per-website expected-CTR curve. Scoring driven by the
`score_components` catalogue under `SCORING_VERSION = "1.0.0"`. Crawl-to-crawl
diffing.

*Done when:* two consecutive crawls of a website where one title was removed
produce exactly one new issue with the correct fingerprint, and restoring the
title flips it to `resolved` — not a new row.

## M7 — Unified dashboard

Home from a single `/overview` call, SEO with its four tabs, issue detail, the
`mark-applied` → verification loop, settings. Every empty and partial state
written.

The unification is the product promise made visible: crawl findings, Search
Console performance and GA4 outcomes in one view, explained.

*Done when:* a new user completes the wizard and lands on a Home screen with a
populated 28-day chart and at least one actionable recommendation, without
seeing a single "no data yet" panel.

## M8 — Weekly report

Issue explanations with the evidence cache, weekly plan generation with the
`last_week` join, output validation, budget admission, metering. HTML email
with score, deltas, priorities, what changed and what was verified.

*Done when:* the numbers validator rejects a deliberately-poisoned generation;
a 500-page crawl costs under $0.20 in explanations after cache warm-up; a
regenerated plan on unchanged data produces the same priorities in the same
order; and the figures in the email match the dashboard exactly for the same
window.

## M9 — AI Strategist

Conversational analysis over typed, read-only tools. It lands last on purpose:
by now there are months of Search Console history, crawl diffs, verified fixes
and weekly plans to reason over. Built at M2 it would have had nothing to say.

*Done when:* "why did my traffic fall?" returns an answer that names the
specific queries and pages responsible, cites its window, and says so plainly
when the data does not support a conclusion.

## Launch readiness (parallel with M8–M9)

Plan limits enforced at admission. Stripe checkout. Partition scheduler
verified three months ahead. Backups with a **tested restore**. Error tracking,
crawl-failure and sync-failure alerting. `/bot` page live. The data-deletion
path from M0 implemented and verified to actually delete.

---

## V1 definition of done (spec §41)

Beta-ready when a new user can do all fourteen, unaided:

- [ ] create an account
- [ ] add a website
- [ ] connect Google
- [ ] authorise Search Console
- [ ] authorise Analytics
- [ ] select the correct properties
- [ ] perform an initial synchronisation
- [ ] crawl their website
- [ ] see search performance
- [ ] see Analytics metrics
- [ ] see technical SEO issues
- [ ] receive AI recommendations
- [ ] return later and see updated data
- [ ] disconnect Google
- [ ] delete their account

The last two are the ones teams routinely defer past beta. Both are compliance
surface, both were promised in the M0 policy documents, and neither is
retrofittable without an awkward conversation.

## Mapping to the spec's sprints (§43)

| Spec sprint | Milestone |
| --- | --- |
| — | **M0** Google Cloud, consent copy, policies, verification submitted |
| 1 Foundation | M1 |
| 2 Google Hub | M2 |
| 3 Search Console | M3 |
| 4 Analytics | M4 |
| 5 Crawler | M5 |
| 6 SEO engine | M6 |
| 7 AI | M7 dashboard · M8 report · M9 assistant |
| 8 Beta hardening | Launch readiness, parallel with M8–M9 |

Phase 3's Business Profile work is decomposed so its external approval cannot
idle unrelated engineering: submit the application now, and let approval and
implementation follow on their own timeline while the rest of Phase 3 proceeds.
See [11-expansion.md](11-expansion.md).

## Sequencing notes

**AI visibility ships as proxies only.** The five computable rules in M6 are
honest and free. Live measurement is phase 3 with its own cost model.

**Competitors and backlinks are not in the MVP** and the screen says so. The
data model exists (`0007`); only acquisition is missing, and the vendor
evaluation runs in parallel without blocking anything.

**What could still go wrong, in rough order of likelihood:** OAuth verification
slipping past M7 (mitigated by starting at M0 and staying inside the test-user
cap); GSC quota during multi-tenant backfill (mitigated by the low-priority
pool and `quota_hits` instrumentation); render-worker memory (mitigated by pool
isolation and hard limits); AI cost per crawl (mitigated by the evidence cache,
measurable from day one through `llm_calls`).
