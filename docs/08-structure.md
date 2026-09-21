# 08 — Repository structure and deployment

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
│   ├── routers/                sites, hub, performance, crawls, issues,
│   │                           plans, keywords, reports, strategist, internal
│   ├── hub/                    ── THE GOOGLE HUB MODULE ──
│   │   ├── oauth.py            PKCE, state, token exchange
│   │   ├── vault.py            envelope encryption, KMS
│   │   ├── discovery.py        sites.list / accountSummaries / locations
│   │   ├── matching.py         resource → site host matching
│   │   ├── clients/            search_console.py, analytics.py, business.py
│   │   └── sync/               backfill.py, incremental.py, quota.py
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
│   │   ├── ctr_baseline.py     per-site expected-CTR curve
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

## Worker pools, separated by job shape

A single queue is how one agency's 40-site backfill makes every other
customer's dashboard look broken.

| Pool | Concurrency | Shape |
| --- | --- | --- |
| `crawl` | 8 | Long, host-rate-limited, resumable |
| `render` | 2 | Memory-heavy, hard limits, restarted often |
| `sync` | 4 | API-quota-bound, back-off-heavy |
| `analysis` | 4 | CPU-bound, short |
| `ai` | 2 | Network-bound, budget-checked |
| `reports` | 2 | Weekly burst |

Backfills run at low priority inside `sync` so a new signup never starves the
nightly incremental that existing customers depend on.

## Deployment

| Piece | MVP | Later |
| --- | --- | --- |
| Postgres | Supabase (managed, PITR) | Self-hosted or Cloud SQL |
| Auth | Supabase Auth (Google + email) | Same, or WorkOS at enterprise |
| Object storage | Supabase Storage | S3/R2 when crawl volume justifies |
| Web | Vercel | Same |
| API + workers | Fly.io or Railway containers | Kubernetes at scale |
| Queue | Postgres (`crawl_frontier` + a jobs table) | Redis/Celery when measured |
| Email | Resend | Same |
| Secrets | Cloud KMS for the master key | Same |

Supabase for the MVP because it collapses Postgres, auth, storage and RLS into
one managed piece, and the schema is plain Postgres DDL so nothing here is a
one-way door. The `secrets` schema is reachable only by the service role.

Redis is deliberately absent from the MVP. The frontier is a Postgres table
with `SKIP LOCKED`, which gives resumability for free and removes an entire
component from the critical path. Add Redis when a measurement demands it.

## CI

On every PR: `ruff` + `mypy`, `pytest` (rules have golden-file tests against
fixture HTML), `tsc` + `eslint`, Playwright smoke test of the wizard against a
mocked Google, and a migration check that applies every migration to a scratch
database from empty. The last one catches the invalid-DDL class of error before
it reaches an environment with data in it.
