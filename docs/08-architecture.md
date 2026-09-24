# 08 — Repository structure and deployment

> **Frozen — architectural source of truth.**
> Changes to this document are architectural decisions, not edits. Amend it
> deliberately, with the reason recorded in
> [10-decisions.md](10-decisions.md).

One repository. Two runtimes, because the split is justified: the analysis and
crawl work is genuinely Python-shaped, and the product surface is genuinely
React-shaped. The boundary is the HTTP API — never a second implementation of
a business rule.

```
.
├── web/                        Next.js 15, App Router, TypeScript
│   ├── app/
│   │   ├── (marketing)/
│   │   ├── (app)/
│   │   │   ├── [siteId]/page.tsx              Home
│   │   │   ├── [siteId]/hub/                  Google Hub
│   │   │   ├── [siteId]/seo/                  Queries/Pages/Opportunities/Technical
│   │   │   ├── [siteId]/competitors/          v2
│   │   │   ├── [siteId]/strategist/           chat (SSE)
│   │   │   └── [siteId]/settings/
│   │   └── onboarding/                        the wizard
│   ├── components/
│   ├── lib/api-client.ts                      generated from the OpenAPI schema
│   └── lib/auth.ts
│
├── api/                        FastAPI
│   ├── main.py
│   ├── deps.py                 auth, org scope, db session, plan limits
│   ├── routers/                websites, hub, performance, crawls, issues,
│   │                           plans, keywords, reports, strategist, internal
│   ├── adapters/               Supabase-specific code lives ONLY here
│   │   ├── auth_supabase.py    JWT verification → user id
│   │   ├── storage_supabase.py object storage
│   │   └── db.py               connection pool, session, GUC binding
│   ├── domain/                 pure logic; no vendor SDK, no framework
│   ├── hub/                    ── THE GOOGLE HUB, A BOUNDED MODULE ──
│   │   ├── routes/             its own FastAPI router
│   │   ├── services/           oauth.py, vault.py, discovery.py, matching.py,
│   │   │                       linking.py, sync/
│   │   ├── providers/
│   │   │   └── google/         search_console.py, analytics.py,
│   │   │                       business_profile.py, ads.py
│   │   ├── models/             connections, resources, links, sync runs
│   │   ├── schemas/            the public contract, versioned
│   │   └── events/             internal domain events published to the core
│   ├── crawler/
│   │   ├── seeds.py            robots.txt, sitemaps
│   │   ├── frontier.py         SKIP LOCKED lease/extend/complete
│   │   ├── fetch.py            httpx + host token bucket
│   │   ├── render.py           Playwright pool, escalation heuristic
│   │   ├── extract.py          per-page extraction
│   │   └── storage.py          object-store keys
│   ├── analysis/
│   │   ├── rules/              one module per rule; the issue catalogue
│   │   ├── fingerprint.py
│   │   ├── ctr_baseline.py     per-website expected-CTR curve
│   │   ├── scoring.py          versioned; SCORING_VERSION constant
│   │   └── diff.py             crawl-to-crawl change detection
│   ├── ai/
│   │   ├── prompts/            versioned prompt files
│   │   ├── router.py           model tier selection
│   │   ├── cache.py            evidence-keyed explanation cache
│   │   ├── validate.py         numbers-in-output must exist in input
│   │   ├── budget.py           per-org admission control
│   │   └── strategist/tools.py typed read-only query tools
│   ├── reports/                weekly HTML/PDF render + delivery
│   └── workers/
│       ├── crawl_worker.py
│       ├── render_worker.py
│       ├── sync_worker.py
│       ├── analysis_worker.py
│       └── scheduler.py        cron: sync, crawl, plan, report, partitions
│
├── db/migrations/              plain SQL, applied in order
├── docs/
└── infra/                      IaC, Dockerfiles, CI
```

## The Hub boundary, enforced

The rule, stated so it is testable rather than aspirational:

> **Only the Hub may construct or call a Google API client.**

That is stronger than "the sync engine talks to Google", because it is a
property a linter can check. The Hub owns OAuth, token lifecycle, property
discovery, the Google API clients themselves, and scheduled sync orchestration.
What crosses the boundary is **normalised Google facts and internal domain
events** — never a client, a credential, or a live API call.

```
Google Hub
  OAuth
  token lifecycle
  property discovery
  Google API clients          <- nothing outside this module may import these
  scheduled sync orchestration
        |
        v
  normalised Google facts  +  domain events
        |
        v
Visibility Core
```

The four domain events are internal to this application. They are **not**
Google webhooks — Google does not push Search Console or Analytics reporting
data to us, and nothing in the design should imply that it does. Every Google
figure in this product arrives because a scheduled job went and asked for it.

```
hub.sync.completed       a sync landed; new facts are queryable
hub.sync.failed          a sync did not land; the gap is recorded
hub.connection.revoked   credentials are gone; stop scheduling work
hub.property.connected   a property was attached to a website
```

The Hub **must not** import: crawler code, SEO scoring, recommendations,
dashboard logic, or competitor intelligence. The dependency runs one way.

```
        ┌──────────────────────┐
        │      Google Hub      │
        │       api/hub/       │
        └──────────┬───────────┘
                   │  normalised facts + domain events
                   ▼
        ┌──────────────────────┐
        │   Visibility Core    │
        │  crawl / SEO / data  │
        └──────────────────────┘
```

The core reads Google data from the normalised tables in `0002`/`0003` and
reacts to `hub.sync.completed`. It never constructs a Google client, and it
never reads `secrets`.

This is checked in CI rather than trusted to reviewers, because an import is
one autocomplete away:

```toml
# .importlinter — run in CI, fails the build on violation
[importlinter]
root_packages = ["api"]

[[importlinter:contract]]
name = "Hub does not depend on the core"
type = "forbidden"
source_modules = ["api.hub"]
forbidden_modules = [
    "api.crawler", "api.analysis", "api.ai", "api.reports", "api.routers",
]

[[importlinter:contract]]
name = "Domain layer is vendor-neutral"
type = "forbidden"
source_modules = ["api.domain"]
forbidden_modules = ["supabase", "gotrue", "postgrest", "storage3"]

[[importlinter:contract]]
name = "Only the Hub may construct a Google API client"
type = "forbidden"
source_modules = [
    "api.crawler", "api.analysis", "api.ai", "api.reports",
    "api.routers", "api.domain",
]
# Top-level packages only — import-linter rejects subpackages of external
# packages. `google` covers google.oauth2, google.auth and google.analytics,
# which is the intent: no Google client of any kind outside the Hub.
forbidden_modules = ["googleapiclient", "google", "google_auth_oauthlib"]
```

The live configuration is in `pyproject.toml` under `[tool.importlinter]`, with
`include_external_packages = true` (required whenever a forbidden module is a
third-party package). Run it with `lint-imports`.

The second contract is what keeps decision 1 real. Supabase is a hosting
choice; if it appears in `api/domain/`, migrating to managed Postgres stops
being a configuration change and becomes a rewrite.

The extraction path this buys: lift `api/hub/` into its own service, point the
core at its URL instead of its Python package, and the product keeps working.
No domain logic moves.

## Worker pools, separated by job shape

A single queue is how one agency's 40-website backfill makes every other
customer's dashboard look broken.

| Pool | Concurrency | Shape |
| --- | --- | --- |
| `crawl` | 8 | Long, host-rate-limited, resumable |
| `render` | 2 | Memory-heavy, hard limits, restarted often |
| `sync` | 4 | API-quota-bound, back-off-heavy |
| `analysis` | 4 | CPU-bound, short |
| `ai` | 2 | Network-bound, budget-checked |
| `reports` | 2 | Weekly burst |

Nightly job schedule (V1 spec §31), staggered by a hash of `website_id` across
each hour so every tenant does not fire at exactly 01:00 and burn Google quota
on retries:

```
01:00  sync_search_console      04:00  calculate_scores
02:00  sync_analytics           04:30  generate_recommendations
03:00  crawl_website            Mon 06:00  generate_weekly_report
```

New accounts run the same jobs immediately through the queue rather than
waiting for the next window.

Backfills run at low priority inside `sync` so a new signup never starves the
nightly incremental that existing customers depend on.

## Deployment

| Piece | MVP | Later |
| --- | --- | --- |
| Postgres | Supabase (managed, PITR) | Self-hosted or Cloud SQL |
| Auth | Supabase Auth (Google + email) | Same, or WorkOS at enterprise |
| Object storage | Supabase Storage | S3/R2 when crawl volume justifies |
| Web | Static export on the container host | Same, behind a CDN |
| API + workers | Containers on one host | Kubernetes at scale |
| Cache, rate limits, OAuth state | Redis (managed) | Same |
| Queue / workers | Celery on Redis | Same |
| Crawl frontier | Postgres `crawl_frontier`, leased with SKIP LOCKED | Same — see below |
| Email | Resend | Same |
| Secrets | Cloud KMS for the master key | Same |

Supabase for the MVP because it collapses Postgres, auth, storage and RLS into
one managed piece, and the schema is plain Postgres DDL so nothing here is a
one-way door. The `secrets` schema is reachable only by the service role.

Redis carries caching, rate limiting, temporary OAuth state and the Celery
broker, as the V1 spec requires.

The **crawl frontier** is the one exception: it stays a Postgres table leased
with `SELECT … FOR UPDATE SKIP LOCKED`. A crawl runs for tens of minutes and
workers get evicted mid-run; with the frontier in Redis a lost worker loses its
in-flight URLs and a Redis restart loses the crawl. In Postgres, an expired
lease returns the rows to `pending` and the crawl resumes at page 400 of 500.
Celery still dispatches the work — Postgres is only the durable record of what
remains to fetch.

## CI

On every PR: `ruff` + `mypy`, `pytest` (rules have golden-file tests against
fixture HTML), `tsc` + `eslint`, Playwright smoke test of the wizard against a
mocked Google, and a migration check that applies every migration to a scratch
database from empty. The last one catches the invalid-DDL class of error before
it reaches an environment with data in it.
