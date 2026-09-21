# 12 — V1 specification conformance

Every section of the V1 specification, and what this repository does about it.

**Status key** — `match`: already specified this way · `adopted`: the spec
changed what was here · `deviation`: implemented differently, reason given, your
call to overrule · `open`: needs a decision or an external step.

---

## 1–3 Product, objective, user journey

| § | Item | Status | Where |
| --- | --- | --- | --- |
| 1 | Name **Visibility Hub**, tagline, six components | adopted | [README](../README.md), [00](00-overview.md) |
| 2 | 5–10 minute onboarding; operator owns all Google Cloud config | match | [04](04-google-hub.md) — the Hub's stated purpose |
| 2 | User performs only Google's consent steps | match | [04 §2](04-google-hub.md) |
| 3 | Journey: landing → account → website → verify → Google → SC/GA4 → sync → crawl → analysis → dashboard → AI | adopted | [07](07-ui.md), [09](09-mvp-sequence.md) |

**Verify website** (§3) was missing and is now a named step. It matters beyond
onboarding polish: ownership verification is what stops the crawler being
pointed at a site the user does not own. GSC linkage is the strongest proof —
if Google says they own it, they own it — with a DNS/file token as fallback.

## 4 Technology stack

| Item | Spec | Here | Status |
| --- | --- | --- | --- |
| Frontend | Next.js + TS, Tailwind, shadcn/ui, TanStack Query, Recharts | same | adopted |
| Backend | Python + FastAPI | same | match |
| Database | PostgreSQL, UUID PKs | same, UUID PKs on all domain entities | match |
| Cache/queue | **Redis** — cache, rate limits, job queues, OAuth state | adopted, with one carve-out below | adopted |
| Workers | **Celery** or equivalent | adopted | adopted |
| Object storage | S3-compatible | adopted (Supabase Storage speaks S3; swappable) | adopted |
| Deploy | Vercel + containers + managed PG/Redis/S3 | adopted | adopted |

**Deviation — the crawl frontier stays in Postgres.** Redis is adopted for
caching, rate limiting, OAuth state and as the Celery broker, exactly as
specified. The *frontier* — the set of URLs discovered but not yet fetched —
stays a Postgres table leased with `SELECT … FOR UPDATE SKIP LOCKED`.

Reason: a crawl runs for tens of minutes and workers get evicted mid-run. With
the frontier in Redis, a lost worker loses its in-flight URLs and a Redis
restart loses the crawl. With it in Postgres, a killed worker's lease simply
expires and the crawl resumes at page 400 of 500. Celery still dispatches the
work; Postgres is the durable record of what remains. Proven by
`db/tests/smoke.sql` test 4.

## 5 High-level architecture

Matches, with the sync layer of §44 made explicit. See
[the architecture diagram](#) and [08](08-structure.md). Worker pools are split
by job shape (crawl / render / sync / analysis / ai / reports) so one tenant's
backfill cannot starve another's nightly sync.

## 6–7 Authentication and OAuth

| § | Item | Status |
| --- | --- | --- |
| 6 | App auth and Google API authorization are separate concerns | match — different tables, different flows, stated explicitly |
| 6 | Google Sign-In + email/password fallback | adopted |
| 7 | Backend owns the flow; authorization code; HTTPS; state; PKCE; incremental auth; encrypted tokens; refresh handling; revocation | match — [04 §2](04-google-hub.md) |

**Deviation — `users.password_hash` is deliberately absent.** Credentials stay
in the auth provider's schema, which the application never queries. Email
/password sign-in works identically, and an application table that cannot leak
a password hash is strictly safer than one that can.

## 8–9 Search Console

| § | Item | Status |
| --- | --- | --- |
| 8 | Discover properties; user selects; never assume the first | match — [04 §3](04-google-hub.md), including the domain-vs-URL-prefix case |
| 9 | Background sync, initial then daily incremental, configurable range | match |
| 9 | Store date, property, query, page, country, device, clicks, impressions, ctr, position | match |

**Deviation — initial sync is 16 months, not 90 days, and the dataset is split
across three tables.**

*On 16 months:* Google deletes this data at 16 months and will not give it back.
Backfilling the maximum on connect costs one slow job once, and gives a
brand-new account a populated year-long trend chart within minutes — which is
the product's activation moment. 90 days is configurable per plan if the
backfill cost proves material.

*On the split:* the Search Console API returns a **different row set per
dimension combination**, and requesting a `query` dimension makes Google
withhold low-volume queries entirely — often 30–50% of clicks. A single
`search_console_daily` table with query and page columns therefore cannot be
populated by one request, and summing it never reconciles with the user's own
Search Console UI. So: `gsc_daily_totals` (unsliced, reconciles exactly),
`gsc_query_daily`, `gsc_page_daily`, and `gsc_query_page_daily` (top queries,
90 days). The `gsc_anonymised_share` view exposes the gap so the UI explains it
rather than the user discovering it. Proven by smoke tests 2 and 3.

## 10–12 Analytics and quotas

| § | Item | Status |
| --- | --- | --- |
| 10 | GA4 Data API, read-only, `analytics.readonly` | match |
| 10 | Property discovery via account summaries | match |
| 11 | Users, sessions, engaged sessions, engagement rate, events, page views, landing pages, country, device, source, medium | adopted — `ga4_dimension_daily` added in `0008` for the dimensional tail |
| 11 | Do not reproduce the GA interface | match — [07](07-ui.md) |
| 12 | Never query Google on dashboard load; sync → database → dashboard | match — the architecture's spine |
| — | UA properties unsupported by the Data API | match — [04](04-google-hub.md) failure table |

**Added, not in the spec:** GA4 key events are arbitrary per property, so the
platform cannot know which event means contact / purchase / donation. The wizard
asks, and `ga4_goal_events` stores the answer. Without it there is traffic data
and no outcome data, and the dashboard must say "outcomes not configured"
rather than show a fabricated conversion rate.

## 13–15 Website onboarding, crawler, crawl data

| § | Item | Status |
| --- | --- | --- |
| 13 | Normalise, validate DNS/HTTP/S, fetch homepage, check HTTPS/redirects/robots/sitemap/canonical/title/description/H1/viewport/language, queue crawl | match — [03](03-crawler.md) |
| 14 | robots.txt, identifying UA, crawl limits, per-host concurrency, trap avoidance, URL normalisation, dedupe, max pages/time/size | match, plus a published `/bot` page and no override flag |
| 14 | Free 100 pages, paid configurable | match — `organizations.max_pages_per_crawl` |
| 15 | Page fields incl. hashes for change detection | match — and two hashes: raw content and extracted text |

**Deviation — page data is append-only per crawl.** The spec models one mutable
row per page; this keeps `pages` as stable identity and `page_snapshots` as one
row per crawl. "What changed since last week" is the entire product, and an
in-place update erases it. The `page_current` view in `0008` returns the spec's
exact shape, so application code reads it as specified.

## 16–17 SEO engine and scoring

| § | Item | Status |
| --- | --- | --- |
| 16 | Critical / high / medium checks | match — full catalogue in [05](05-analysis-scoring.md), superset of the list |
| 17 | Own clearly-labelled **Visibility Health Score**, methodology documented | adopted — components renamed to Technical Health, Search Performance, Content Health, Analytics Coverage |
| 17 | Never imply it is a Google score | match — stated in the schema comment and the UI copy rules |

Weights under `scoring_version = '1.0.0'`: 0.30 / 0.35 / 0.25 / 0.10. AI
visibility and Authority exist as disabled components awaiting data sources, so
the scorecard omits them rather than showing a zero. Smoke test 7 asserts the
enabled weights total exactly 1.00.

**Added:** a CTR finding must compare against expected CTR *at that position*.
0.77% is dreadful at position 3 and normal at position 18; flagging raw low CTR
produces hundreds of non-issues and teaches users to ignore the list.

## 18–20 Dashboard, search performance, audit

All three match [07](07-ui.md). Filters, chart set, and table columns as
specified. The audit issue detail carries problem / why it matters / affected
pages / recommended action / `[Mark resolved]`; `issues.resolved_by` and
`resolution_source` record whether a human marked it or a verification crawl
proved it.

## 21–25 AI

| § | Item | Status |
| --- | --- | --- |
| 21 | Structured evidence in, explanation out; never "look at the website" | match — [06](06-ai-layer.md) |
| 22 | Title / priority / evidence / problem / why / action / expected measurement | match |
| 22 | Never guarantee rankings | match — enforced in prompt text and reviewed in output validation |
| 23 | Assistant over website, crawl, GSC, GA4, audit, history; no raw credential access | match — typed read-only tools, org scope bound server-side |
| 24 | Eight guardrails | match — and a post-generation validator that extracts every number from output and asserts it appeared in the input evidence |
| 25 | `recommendations` table and OPEN/IN_PROGRESS/RESOLVED/DISMISSED | adopted |

**Added:** priority is computed in code, not chosen by the model, and the unit
is estimated additional monthly clicks. A generative ranker produces a different
top four on Tuesday than Monday and can explain neither.

## 26–29 Database schema

Table and column vocabulary adopted throughout: `users`, `organizations`,
`organization_members`, `websites`, `connections`, `connection_properties`,
`pages`, `recommendations`. UUID primary keys on every domain entity.

Two deviations, both above: the Search Console split (§9) and page snapshots
(§15).

**Deviation — provider-agnostic connection tables.** The spec names these
`google_connections` / `google_properties`. They are `connections` /
`connection_properties` with a `provider_key`, because the expansion list adds
Business Profile, Ads, WordPress and social — all of which are "an account the
user authorises, exposing properties, some attached to a website". Renaming
before launch is free; after launch it is a migration plus a code sweep. The
Google-only reading is unchanged: every V1 row has `provider_key = 'google'`.

**Deviation — `bigserial` on append-only logs.** UUIDs on all domain entities as
specified. `issue_observations`, `llm_calls`, `audit_log`, `backlink_changes`
and `alert_events` use `bigserial`: they are internal logs reaching millions of
rows, where sequential keys give materially smaller indexes and better insert
locality. None is ever exposed in a URL.

## 30 API design

Adopted wholesale — `/api/v1` with the spec's route names. See [02](02-api.md).

## 31 Background jobs

All six adopted: `crawl_website`, `sync_search_console`, `sync_analytics`,
`calculate_scores`, `generate_recommendations`, `generate_weekly_report`, on the
01:00–05:00 schedule, run immediately via the queue for new users.

**One change:** the nightly times are staggered by a hash of `website_id` across
each hour rather than firing at exactly 01:00 for every tenant, which would
thunder-herd the Google APIs and burn quota on retries.

## 32–33 Frontend routes and onboarding screens

Adopted exactly, including the seven onboarding screens and the progress
checklist. See [07](07-ui.md).

## 34–37 Security, multi-tenancy, privacy, disconnect

| § | Item | Status |
| --- | --- | --- |
| 34 | Tokens encrypted at rest with managed KMS | match — envelope encryption in a separate `secrets` schema with no policies and no grants |
| 34 | No secrets in source control | match |
| 34 | Secure HTTP-only cookies, CSRF, OAuth state/redirect/issuer/expiry validation | match |
| 34 | Every query scoped server-side; never trust ids from the browser | match — handlers derive scope from the session, plus RLS as defence in depth |
| 35 | User → organization → website → connections → data | match — smoke test 1 asserts a user cannot see another org's website |
| 36 | Privacy policy, ToS, DPA, Google disclosure, deletion, revocation | match — frozen in M0 |
| 37 | Disconnect stops sync, revokes tokens, marks inactive, explains data retention | match |

## 38–40 Errors, observability, testing

All adopted. The spec's five error messages are the canonical copy; the error
contract in [02](02-api.md) carries a stable `code` the UI branches on plus a
`message` written for a non-technical reader. Logging explicitly never includes
tokens. The unit / integration / end-to-end split is adopted, and the schema
smoke tests in `db/tests/` already cover permissions and data transformations.

## 41 Definition of done

Adopted verbatim as the V1 acceptance checklist in [09](09-mvp-sequence.md),
including "return later and see updated data", "disconnect Google" and "delete
their account" — the last two being the ones teams routinely defer past beta.

## 42 What not to build in V1

All eleven confirmed out of scope. Each has a designed seam so it plugs in
without a rewrite — see [11](11-expansion.md). Nothing on the list has UI
surface in V1, and `score_components` keeps Authority and AI visibility disabled
so the scorecard omits them rather than rendering a zero.

## 43 Development sequence

| Spec sprint | Milestone here |
| --- | --- |
| — | **M0** Google Cloud project, consent copy, policies, verification submitted |
| 1 Foundation | M1 |
| 2 Google Hub | M2 |
| 3 Search Console | M3 |
| 4 Analytics | M4 |
| 5 Crawler | M5 |
| 6 SEO engine | M6 |
| 7 AI | M7 dashboard, M8 report, M9 assistant |
| 8 Beta hardening | Launch readiness, parallel with M8–M9 |

**One change:** M0 sits before Sprint 1. Sensitive-scope verification takes
weeks and bounces at least once, and the spec itself (§43, §36) says not to
treat it as an afterthought. Registering the project and publishing the privacy
policy costs nothing on day one and is the longest-lead item in the plan.

**One split:** Sprint 7's AI work is three milestones, because the assistant
(§23) reasons over accumulated history. Shipped alongside the recommendation
engine it has weeks of data; shipped after the weekly report it has months.

## 44 The most important architectural decision

Agreed and structural: nothing in the read path touches a Google API. The
dashboard reads Postgres only. If Google is unavailable, the product serves the
last sync and says when it was. Enforced by the module boundary — the core has
no Google client to call.

## 45 V1 boundary

Matches. The remaining open items are commercial, not architectural, and are
listed in [10](10-decisions.md).
