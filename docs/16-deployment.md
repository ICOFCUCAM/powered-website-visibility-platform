# Deployment

[08-architecture.md](08-architecture.md) decided the shape and it has not
changed: **this is eight long-running processes and a static site.** All of
them run as containers on one host. This document is how, and in what order.

## Why a serverless host cannot hold this

The question came up because the front end deploys to one so cleanly, and for
a while it did — with the other seven eighths of the system somewhere else.
The rest does not fit, and not by a small margin:

- **Celery beat and six worker pools are long-running processes.** There is no
  such thing to deploy to a function host. The nightly schedule, the crawler,
  the report builder and the alert scan all live in them.
- **A crawl runs for tens of minutes.** Function timeouts cap far below that.
- **The connection pool opens once at startup and is reused** for the process's
  life. Per-invocation processes would churn it, and Postgres connection limits
  are the first thing that breaks under that pattern.
- **The crawl frontier is leased with `SELECT … FOR UPDATE SKIP LOCKED`** by a
  worker that holds the lease across a whole crawl. There is nothing to hold it
  in a function that has returned.

Splitting the front end off to its own host bought nothing for this, and cost
a second place to configure, a second place for an origin to be wrong, and a
CORS boundary between two halves of one product. So: one container host for
all of it. `fly.toml` in the repository root is a worked example of the
process set; DeployPro runs the same set from its own dashboard.

## What runs where

| Piece | Host | Notes |
| --- | --- | --- |
| `web/` | Container, static | `output: 'export'` — every route prerenders, so a web server serves files and no Node runs |
| API | Container, 1+ instances | Reachable from the internet, like `web/` |
| `beat` | Container, **exactly one** | Never scale it to two |
| `crawl` `sync` `analysis` `ai` `reports` | Container, one service each | Concurrencies in `fly.toml` |
| Postgres | Supabase | With PITR on, and a restore you have actually tested |
| Redis | Managed | Cache, rate limits, OAuth state, Celery broker |
| Object storage | Supabase Storage | Raw crawled HTML |
| Email | Resend or any SMTP | Weekly report and customer alert notices |

**Run the pools apart.** A single worker across all queues is how three hung
Google syncs starve every score, plan and report behind them — which is
exactly what a smoke run of the scheduler did before they were separated.

**Concurrency is a database connection count, not just a memory number.**
`api/workers/app.py::_open_pools` runs on `worker_process_init`, so every
forked child opens its own pools — and there are two, the request-path pool
as `app_user` and the service pool as `app_service`. A pool's *floor* is one
connection each, which `open(wait=True)` blocks on, so a worker started with
`-c 8` needs sixteen connections before it will serve anything, and
`DB_POOL_MAX` bounds only the ceiling above that.

Add the concurrencies up and double the total before choosing a database.
The set in `fly.toml` (8 + 4 + 4 + 2 + 2) is twenty processes and forty
connections at rest — fine against a dedicated Postgres, and more than
Supabase's session pooler will hand out on a small project. The symptom is
`psycopg_pool.PoolTimeout: pool initialization incomplete after 10 sec` on
whichever process starts once the budget is gone, which reads like a network
fault and is arithmetic. Halve the concurrencies before you halve the pools.

## Environment

Every process gets the same image and differs only in its command, so the
variables are close to the same too. `.env.example` is the full list; this is
which process actually needs what.

| Variable | API | Workers | Beat |
| --- | :-: | :-: | :-: |
| `DATABASE_URL` (as `app_user`) | ● | ● | ● |
| `SERVICE_DATABASE_URL` (as `app_service`) | ● | ● | ● |
| `JWT_SECRET`, `JWT_AUDIENCE` | ● | | |
| `CORS_ORIGINS` | ● | | |
| `ENVIRONMENT=production` | ● | ● | ● |
| `REDIS_URL` | ● | ● | ● |
| `GOOGLE_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | ● | ● | |
| `TOKEN_MASTER_KEY` (from KMS) | ● | ● | |
| `WEB_BASE_URL` | ● | ● | |
| `ANTHROPIC_API_KEY` | ● | ● | |
| `SMTP_*`, `REPORT_FROM_EMAIL` | ● | ● | |
| `ALERT_WEBHOOK_URL`, `ALERT_EMAIL` | | ● | |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | ● | | |

Two roles, and the distinction is not cosmetic: the API connects as `app_user`
with RLS applied, and only the vault and the workers connect as `app_service`,
which is `BYPASSRLS` and the sole role with access to the `secrets` schema. A
deployment that points `DATABASE_URL` at the service role has turned off every
tenancy guarantee in the system and nothing will complain.

### CORS is the one that will bite you

The front end and the API are two different origins even on one host: the
browser holds the site, and the site calls an API on another name. Same
machine, same network, still cross-origin. `CORS_ORIGINS` is a
comma-separated list of the origins allowed to call the API:

```
CORS_ORIGINS=https://app.example.com
```

In production it has **no default**, for the same reason no secret does. A
localhost fallback would not be a degraded mode; it would be a deployment
where every request is blocked and the error appears in a customer's browser
console rather than in your logs. `*` is refused outright, and so is plain
`http`. It is a list because more than one origin can legitimately need in —
a staging front end, or a preview deployment where the host gives each one
its own name. Include the ones you actually want working, and no others.

The session is a bearer token in `localStorage`, not a cookie, so
`allow_credentials` is off. If that ever changes to cookie sessions, the
origin list becomes the only thing standing between a customer's session and
any site that can get them to load a page.

## Blocker: the artifact store is still local disk

**This has to be fixed before the first production crawl, not after.**

`ARTIFACT_ROOT` defaults to `var/artifacts` and is served by
`LocalArtifactStore` — a filesystem directory. That is correct for
development and wrong the moment the crawl worker and the API are different
machines, which on any container host they are:

- The crawl worker writes fetched HTML to **its own** ephemeral disk. It is
  gone on the next deploy.
- **Account deletion runs in the API container.** `_delete_objects` removes a
  prefix from a directory that, over there, is empty. The receipt would record
  `objects: 0` — literally true about what the API deleted, and false about
  what happened: the customer's fetched HTML is still sitting on the crawl
  worker. [14-deletion.md](14-deletion.md) promises that deleting an account
  reaches the fetched HTML. With a local store on two machines it does not.

Nothing *functionally* depends on reading the HTML back — `raw_key` is
written and never read; analysis works from the parsed `page_snapshots`
columns, and a report whose artifact is missing rebuilds from its stored
payload. So this does not break the product. It breaks a deletion guarantee,
which is worse, because it fails silently and looks like it worked.

The fix is the one the code was shaped for. `ArtifactStore` is three methods
— `put`, `get`, `delete_prefix` — and `LocalArtifactStore`'s own docstring
says "the S3 implementation replaces this class and nothing else". It needs:

- a Supabase Storage or S3/R2 bucket, private,
- an `S3ArtifactStore` next to `LocalArtifactStore`,
- both call sites (`api/workers/jobs.py::artifact_store` and
  `api/account/routes.py::_store`) choosing it from the environment,
- and a test that deletion's `delete_prefix` actually reaches it.

Until that exists, either do not run the crawler in production, or accept
that "delete my account" leaves fetched HTML behind — which is not an
acceptable thing to accept.

## The front end, specifically

- **Set the project's root directory to `web/`. This is not a preference.**
  The repository root holds the API's `Dockerfile`, and a build that starts
  there builds the API — successfully, which is the problem: the deployment
  goes green and serves a second copy of the API on the address meant for the
  site. Pointing the root directory at `web/` is what makes the build see a
  Next project at all.
- **`NEXT_PUBLIC_API_BASE_URL` is a build-time value**, inlined into the
  bundle by `next.config.ts`. Changing it needs a redeploy, not an environment
  edit. Set it to the API's origin *including* the version prefix:
  `https://api.example.com/api/v1`.

  It must be a **permanent** domain. A per-deployment hostname works until the
  API is deployed again, and then the front end — already built, already
  serving — keeps asking an address that no longer answers. The symptom is a
  site that renders perfectly and cannot log anyone in.
- **`output: 'export'` means no server.** The build emits `out/` and a web
  server serves it. Two consequences worth knowing before someone adds a
  feature that needs one: `headers()` in `next.config.ts` does nothing, so the
  response headers come from whatever serves the files, and adding a route
  handler, middleware, a dynamic segment or server-side fetching will fail the
  build rather than silently changing the deployment shape.
- Nothing else. There are no route handlers, no middleware and no server-side
  fetching to configure.

## On DeployPro, concretely

Two projects from this one repository, because the front end and the API are
different builds with different root directories.

**Project 1 — the API**, root directory empty, so the build uses the
repository's `Dockerfile`. Its web process is the image's own command. Then
six more processes, all from the same image, differing only in command — which
is the point: a worker and the API are running the same code by construction,
not because someone remembered to deploy both.

| Name | Type | Replicas | Command |
| --- | --- | :-: | --- |
| `beat` | worker | **1, never 2** | `celery -A api.workers.app:app beat --loglevel INFO` |
| `crawl` | worker | 1 | `celery -A api.workers.app:app worker -Q crawl -c 8 --loglevel INFO -n crawl@%h` |
| `sync` | worker | 1 | `celery -A api.workers.app:app worker -Q sync -c 4 --loglevel INFO -n sync@%h` |
| `analysis` | worker | 1 | `celery -A api.workers.app:app worker -Q analysis -c 4 --loglevel INFO -n analysis@%h` |
| `ai` | worker | 1 | `celery -A api.workers.app:app worker -Q ai -c 2 --loglevel INFO -n ai@%h` |
| `reports` | worker | 1 | `celery -A api.workers.app:app worker -Q reports -c 2 --loglevel INFO -n reports@%h` |

Beat is the clock and there must be exactly one. Two would double every tick.
The slot claim in `scheduled_runs` would still keep the work single, but there
is no reason to make the database referee something a replica count already
settles.

The API needs a **permanent domain** before the front end is built, because of
`NEXT_PUBLIC_API_BASE_URL` above. Add it to the project and let DNS verify
before deploying anything that points at it.

**Project 2 — the front end**, root directory `web/`, one web process, one
variable: `NEXT_PUBLIC_API_BASE_URL=https://<the API's domain>/api/v1`. It
builds to static files, so it needs no other environment at all. Then put its
origin in the API's `CORS_ORIGINS` and redeploy the API — the front end cannot
call it until that is done, and the failure shows up in the browser console
rather than in any log you are watching.

### Redis

Nothing in this repository starts it and DeployPro does not manage datastores,
so it is one container on the same network:

```
docker run -d --name redis --restart unless-stopped \
  --network deploypro \
  -v redis-data:/data \
  redis:7-alpine redis-server \
    --requirepass "$REDIS_PASSWORD" \
    --appendonly yes \
    --maxmemory 192mb --maxmemory-policy noeviction
```

Then `REDIS_URL=redis://:<password>@redis:6379/0` on every process. It carries
no DeployPro labels, so the platform's housekeeping — which filters everything
it touches on `deploypro.owner` — will not sweep it.

The password is not optional on a shared network: every other deployment on
the host can reach `redis:6379`, and the Celery queue is not something another
project should be able to read or write.

### Sizing

Seven Python processes, a web server and Redis do not fit in 4 GB with room to
build. The crawl pool is the hungriest — it holds fetched HTML in memory while
parsing — so if something has to give, cut its concurrency before merging
pools. Merging `analysis`, `ai` and `reports` into one worker is the next
cheapest concession and it costs what "run the pools apart" above says it
costs. Anything below that, add memory rather than argue with the design.

## Migrations

`db/migrations/*.sql` are applied in filename order by a role that owns the
schema — never by `app_user` or `app_service`. Run them as a release step
before the new image takes traffic:

```
for f in db/migrations/*.sql; do
    psql "$ADMIN_DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"
done
psql "$ADMIN_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/roles.sql
```

They are written to be re-runnable. `db/tests/smoke.sql` asserts the tenancy
and privilege guarantees directly against a built database and is worth
running against staging after any migration: 45 assertions, and the ones that
matter are about what a client role *cannot* do.

## The order that matters

Deploy before the product is finished, because one item on the list takes
weeks and nothing else shortens it.

1. **Supabase project**, then apply the migrations and `db/roles.sql`, in that
   order — `roles.sql` grants on `app` and `secrets` and calls
   `app.lock_derived_columns()`, none of which exist until the migrations have
   run. The one exception is the two `create role` statements, which have to
   come first: migrations 0012, 0014 and 0021 apply column protections to
   `app_user` and each skips silently if the role is absent.

   Turn on PITR and test a restore — a backup you have not restored is a
   belief, not a backup.

   **Then check `anon` reaches nothing.** Supabase grants `anon` and
   `authenticated` full DML on every table created in `public`, and PostgREST
   publishes each one at `/rest/v1/<table>` to anybody holding the publishable
   key. RLS covers the tenant tables; it does not cover a *partition*, which
   carries no policies of its own — so `gsc_query_daily_202609` was readable
   and writable while `gsc_query_daily` was protected, and a fresh one appears
   every month. `0023_postgrest_exposure.sql` takes both roles off the schema
   entirely, which is the only version of this fix that stays correct as
   tables are added. `test_rls_backstop.py` asserts the outcome.
2. **Redis.** Managed, or a container on the same host and network as
   everything else. It is not optional and it is not only the Celery broker:
   the API reaches it for peek rate limits (`api/peek/limits.py`) and for
   OAuth state (`api/hub/services/oauth_state.py`). `REDIS_URL` is required at
   startup and the API now pings it there (`api/adapters/cache.py`), so a
   wrong value fails the deploy instead of failing a customer's first "scan my
   site". Give it a password: on a shared container network, no password means
   every other deployment on the host can read the queue.

   Set `maxmemory-policy noeviction`. A broker whose messages can be evicted
   under memory pressure loses jobs silently, which is the worst way to lose
   them.
3. **Object storage**, and the `S3ArtifactStore` that goes with it — see the
   blocker above. This is the one step that is code rather than an account.
4. **API and workers** to the container host, with `CORS_ORIGINS` pointing at
   the domain you are about to use. Check `/api/v1/health`.
5. **`web/`**, root directory `web/`, pointed at the API's permanent domain.
6. **Google Cloud project** with the real redirect URI, and **submit for OAuth
   verification**. This is the long pole: sensitive scopes cap you at 100 test
   users until approved, and review takes weeks. It cannot start against
   `localhost`, which is why it comes after a deploy rather than before one.

Steps 1, 2, 4 and 5 are an afternoon. Step 3 is a day. Step 6 is the
calendar. Everything still open — Stripe, the `/bot` page, error tracking —
can be built while Google reviews.

## After it is up

The queries worth having on a dashboard from day one:

```sql
-- alerting itself is broken: an incident opened but never delivered
select * from operator_alerts
 where resolved_at is null and notified_at is null
   and first_seen_at < now() - interval '1 hour';

-- last night, by outcome
select job, status, count(*) from scheduled_runs
 where window_start > now() - interval '1 day' group by job, status;
```

Set `ALERT_WEBHOOK_URL` before the first night runs, not after. Until it is
set nothing is delivered — which is logged loudly rather than assumed handled,
but a log nobody is reading is not monitoring. See
[15-alerting.md](15-alerting.md).

## Not yet

**Error tracking.** A crash in the request path is logged and nothing more.
That wants a real tracker with releases and stack traces, and is the last
launch-readiness item with no code behind it.

**CI/CD.** Nothing deploys automatically. `./scripts/check.sh` is what CI
would run; wiring it to a push and a deploy is a small job that is not worth
doing until the hosts above exist and their names are known.
