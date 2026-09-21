# 01 — Data model

DDL lives in [`db/migrations/`](../db/migrations/). This document explains why
the tables are shaped the way they are. Read the reasoning before changing the
schema; most of it is expensive to reverse.

| Migration | Contents |
| --- | --- |
| `0001_tenancy.sql` | orgs, profiles, org_members, sites |
| `0002_google_hub.sql` | token vault, google_accounts, google_resources, links, sync runs |
| `0003_google_data.sql` | GSC and GA4 fact tables, correct-aggregation views |
| `0004_crawl.sql` | crawls, frontier, pages, page_snapshots, links, PSI samples |
| `0005_analysis.sql` | issues, observations, keywords, scores, plans, fixes, metering |
| `0006_rls.sql` | RLS policies, partition management |

## The five load-bearing decisions

### 1. Identity and observation are separate tables

`pages` is one row per URL, forever. `page_snapshots` is one row per URL per
crawl, append-only. The same applies to `issues` / `issue_observations` and to
`score_snapshots`.

If a crawl updated a page row in place, the platform could render today's state
and nothing else — no trend, no "what changed", no proof a fix held. The trend
line is what the customer pays for, so nothing that a customer sees over time is
ever mutated.

### 2. `org_id` is on every tenant table

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
`gsc_anonymised_share` exposes the gap so the UI can explain it. Deriving site
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
**resources** across services, which are **linked** to sites:

```
google_accounts   1 ── n   google_resources   1 ── n   site_google_links
  (one per            (GSC property,             (which resource
   Google account)     GA4 property,              feeds which site)
                       GBP location,
                       Ads customer)
```

This is what lets a single "Continue with Google" discover Search Console,
Analytics and Business Profile in one pass, and what lets the Hub be lifted out
as a standalone product: nothing in these four tables knows about crawling,
issues or scoring.

`google_resources.matched_hosts` is the auto-matching key. A GSC domain
property `sc-domain:example.com` expands to `{example.com}`; a URL-prefix
property `https://www.example.com/` expands to `{www.example.com}`. The wizard
proposes a link when a resource's hosts intersect the site's domain, and always
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
