# 01 — Data model

DDL lives in [`db/migrations/`](../db/migrations/). This document explains why
the tables are shaped the way they are. Read the reasoning before changing the
schema; most of it is expensive to reverse.

| Migration | Contents |
| --- | --- |
| `0001_tenancy.sql` | organizations, users, organization_members, websites |
| `0002_connections.sql` | token vault, connections, connection_properties, links, sync runs |
| `0003_google_data.sql` | GSC and GA4 fact tables, correct-aggregation views |
| `0004_crawl.sql` | crawls, frontier, pages, page_snapshots, links, PSI samples |
| `0005_analysis.sql` | issues, observations, keywords, scores, plans, fixes, metering |
| `0006_rls.sql` | RLS policies, partition management |

## The five load-bearing decisions

### 0. System of record

| Store | Is the system of record for |
| --- | --- |
| PostgreSQL | Application state, and synchronised observations |
| Object storage | Immutable raw artefacts — fetched HTML, extraction documents, rendered reports |
| Google | Google-originated **measurements**. Our copy is a synchronised observation of them, not a competing truth |

That last row settles an argument before it happens. If a figure here disagrees
with Search Console, Search Console is right and our sync is behind or
incomplete — and `sync_runs` plus `gsc_anonymised_share` exist to say which.
The database is authoritative for *what we observed and when*; it is never
authoritative for what Google measured.

### 1. Mutability is classified, not assumed

"Append-only" is a property of **observations**, not of every table. Applying it
literally to `users` or `websites` would be absurd. Three tiers, and every table
belongs to exactly one:

**Tier 1 — append-only observations.** Written once, never updated, never
deleted outside retention policy. These are the history the product sells.

```
gsc_daily_totals      gsc_query_daily       gsc_page_daily
gsc_query_page_daily  ga4_daily             ga4_page_daily
ga4_dimension_daily   ga4_goal_daily        page_snapshots
issue_observations    score_snapshots       backlink_changes
psi_samples           llm_calls             audit_log
```

**Tier 2 — append-per-attempt, with a lifecycle status.** One row per attempt,
created once. The status advances through its lifecycle; a retry or a reversal
creates a **new row** rather than overwriting the old one, so the attempt
history survives.

```
crawls        sync_runs        actions        plans        reports
```

`actions` is the clearest case: a revert does not flip the original row back,
it inserts a new action pointing at the one it undoes via `reverts_action_id`.

**Tier 3 — mutable operational state.** Updated in place, as normal.

```
users         organizations    organization_members    websites
connections   connection_properties    website_connections
keywords      issues.status    recommendations.status  crawl_frontier
alert_rules   content_drafts   organization_branding
```

Note `issues` appears in tier 3 while `issue_observations` is tier 1. That is
the design: the issue row carries current status, which changes; the
observation rows carry what was true at each crawl, which does not. Deleting
the distinction is how a product loses the ability to prove a fix held.

### 1b. Identity and observation are separate tables

`pages` is one row per URL, forever. `page_snapshots` is one row per URL per
crawl. The same split applies to `issues` / `issue_observations`.

If a crawl updated a page row in place, the platform could render today's state
and nothing else — no trend, no "what changed", no proof a fix held.
`page_current` (migration `0008`) returns the single-row-per-page shape over
that history for code that wants it.

### 1c. Every derived number carries provenance

Six months in, someone points at a number on the dashboard and asks where it
came from. With Google data, crawler data, vendor data, modelled estimates,
calculated scores and AI-written explanations in one view, that question is
unanswerable unless the answer was recorded when the number was computed.

So every derived table carries the same contract (migration `0009`):

| Column | Meaning |
| --- | --- |
| `source` | `search_console` · `analytics` · `crawl` · `vendor` · `modelled` · `derived` · `user` |
| `derived_from` | the specific records, by id or key |
| `observed_from` / `observed_to` | the window of source data summarised |
| `computed_at` | when this value was calculated |
| `calculation_version` | which rule, score or prompt version produced it |

`calculation_version` is what makes a value reproducible: the same inputs under
the same version must produce the same output, and a version bump is the only
legitimate reason for a historical number to change.

`provenance_index` answers the question across every derived table in one
query. Smoke tests 9 and 10 assert the columns exist and that a score traces
back to the crawl that produced it — because a contract like this erodes one
convenient migration at a time unless something fails the build.

And the invariant that goes with it:

> **AI may explain and prioritise existing evidence. It may not create
> evidence.**

Every figure in generated prose comes from a value passed into the prompt, and
a post-generation validator rejects any number in the output that did not
appear in the input.

### 2. `organization_id` is on every tenant table

Denormalised deliberately. It makes each RLS policy a single indexed predicate
instead of a three-table join, and it makes a missing tenant filter a visible
omission rather than a subtle one. Retrofitting tenancy is the most painful
migration there is; every table has it from the first migration.

### 3. Refresh tokens live in their own schema with no policies

`secrets.oauth_tokens` holds envelope-encrypted ciphertext: the data key is
wrapped by a KMS master key, and the plaintext token exists only inside the
process that is making the Google call. The schema has no RLS policies and no
grants, so no client role can read it under any circumstance — not through a
view, not through an ORM relation, not through a PostgREST expansion. A
compromised client key leaks tenant data, which is bad; it must not also hand
over the ability to read a customer's Google account.

### 4. GSC query rows do not sum to GSC totals — the schema says so

`gsc_daily_totals` is fetched unsliced. `gsc_query_daily` is fetched with a
query dimension, which causes Google to withhold low-volume queries entirely —
commonly 30–50% of clicks. They are different tables on purpose, and
`gsc_anonymised_share` exposes the gap so the UI can explain it. Deriving website
totals by summing query rows produces numbers that contradict the user's own
Google account, which is the fastest way to lose a sophisticated customer.

Related: `position` is an impressions-weighted average within its row's bucket.
`avg(position)` across rows is wrong in a way that looks plausible. Use
`gsc_query_rollup` / `gsc_page_rollup`, which do
`sum(position * impressions) / sum(impressions)`.

### 5. Issues have fingerprints, not autoincrement identities

```
fingerprint = sha256("{type_key}:{scope_type}:{scope_ref}")
```

Deterministic, so "missing title on /about-us" is the same row in week 1 and
week 40. That identity is what makes the lifecycle possible:

```
open → applied → verified            (fix confirmed by a later crawl)
     → regressed                     (it came back)
     → dismissed / snoozed           (user's call, remembered)
     → resolved                      (gone, and stayed gone)
```

Detection is deterministic rule code that emits fingerprints. The model writes
the explanation and the how-to. It never decides what is broken, because a
generative detector produces a different list on Tuesday than on Monday and the
loop stops being verifiable.

## The Google Hub's shape

One connected **account** grants a set of **scopes**, which expose
**resources** across services, which are **linked** to websites:

```
connections   1 ── n   connection_properties   1 ── n   website_connections
  (one per            (GSC property,             (which resource
   Google account)     GA4 property,              feeds which website)
                       GBP location,
                       Ads customer)
```

This is what lets a single "Continue with Google" discover Search Console,
Analytics and Business Profile in one pass, and what lets the Hub be lifted out
as a standalone product: nothing in these four tables knows about crawling,
issues or scoring.

`connection_properties.matched_hosts` is the auto-matching key. A GSC domain
property `sc-domain:example.com` expands to `{example.com}`; a URL-prefix
property `https://www.example.com/` expands to `{www.example.com}`. The wizard
proposes a link when a resource's hosts intersect the website's domain, and always
lets the user override — `link_method` records which happened, so bad
auto-matches are findable later.

## The frontier is the queue

`crawl_frontier` is a Postgres table, not a Redis list. Workers lease with:

```sql
update crawl_frontier f
set state = 'leased', leased_until = now() + interval '5 minutes',
    attempts = attempts + 1
where (f.crawl_id, f.url_hash) in (
    select crawl_id, url_hash from crawl_frontier
    where crawl_id = $1 and state = 'pending'
    order by depth, url_hash
    limit $2
    for update skip locked
)
returning f.url, f.depth, f.url_hash;
```

A killed worker's lease expires and the rows return to `pending`. A crawl that
dies at page 400 of 500 resumes at 400. This is the cheapest way to get the
resumability and idempotence that long crawls require, and it removes Redis
from the MVP's critical path entirely.

## Partitioning

Monthly range partitions on `gsc_query_daily`, `gsc_page_daily`,
`gsc_query_page_daily`, `ga4_page_daily`, `ga4_goal_daily` and
`page_snapshots`. `app.ensure_partitions_ahead(3)` runs on a schedule and must
never fall behind — a missing partition is an INSERT error, not a silent drop.

Retention: raw objects in cold storage after 90 days;
`gsc_query_page_daily` pruned to 90 days (it is the largest and least queried);
everything else kept indefinitely. **Never prune `gsc_query_daily`.** Google
deletes it at 16 months and the platform does not have to — two years in, that
history is data the customer cannot obtain anywhere else.

## Tables deliberately not created yet

| Table | Waits for | Why not now |
| --- | --- | --- |
| `competitors`, `competitor_snapshots` | v2 | Needs a licensed keyword/traffic source |
| `backlinks`, `referring_domains` | v2 | A link index is licensed, never built |
| `ai_visibility_queries`, `ai_visibility_runs` | v3 | Spec'd in [05](05-analysis-scoring.md); must store per-run results to report a mention *rate*, not a boolean |
| `subscriptions`, `invoices` | v2 | Stripe is the source of truth; mirror only what the UI needs |

Adding an empty table for a module with no data source invites the UI to render
a zero, and a zero the platform cannot defend is worse than an absence.
