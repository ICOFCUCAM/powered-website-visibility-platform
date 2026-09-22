# Deployment

[08-architecture.md](08-architecture.md) decided the shape and it has not
changed: **the web app goes to Vercel, the API and workers go to containers.**
This document is how, and in what order.

## Why not all of it on Vercel

The question comes up because the front end deploys there so cleanly. The rest
does not, and not by a small margin:

- **Celery beat and six worker pools are long-running processes.** Vercel has
  no such thing. The nightly schedule, the crawler, the report builder and the
  alert scan all live in them.
- **A crawl runs for tens of minutes.** Serverless functions cap far below
  that.
- **The connection pool opens once at startup and is reused** for the process's
  life. Per-invocation processes would churn it, and Postgres connection limits
  are the first thing that breaks under that pattern.
- **The crawl frontier is leased with `SELECT … FOR UPDATE SKIP LOCKED`** by a
  worker that holds the lease across a whole crawl. There is nothing to hold it
  in a function that has returned.

So: Vercel for `web/`, a container host for everything else. Fly.io and
Railway both work; `fly.toml` in the repository root is a worked example.

## What runs where

| Piece | Host | Notes |
| --- | --- | --- |
| `web/` | Vercel | Static: every route prerenders, nothing renders on a server |
| API | Container, 1+ instances | The only process reachable from the internet |
| `beat` | Container, **exactly one** | Never scale it to two |
| `crawl` `sync` `analysis` `ai` `reports` | Container, one service each | Concurrencies in `fly.toml` |
| Postgres | Supabase | With PITR on, and a restore you have actually tested |
| Redis | Managed | Cache, rate limits, OAuth state, Celery broker |
| Object storage | Supabase Storage | Raw crawled HTML |
| Email | Resend or any SMTP | Weekly report and customer alert notices |

**Run the pools apart.** A single worker across all queues is how three hung
Google syncs starve every score, plan and report behind them — which is
exactly what a smoke run of the scheduler did before they were separated.

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

The front end and the API are two different origins the moment you deploy —
that is not a quirk of this setup, it is what putting the app on Vercel means.
`CORS_ORIGINS` is a comma-separated list of the origins allowed to call the
API:

```
CORS_ORIGINS=https://app.example.com,https://visibility-hub.vercel.app
```

In production it has **no default**, for the same reason no secret does. A
localhost fallback would not be a degraded mode; it would be a deployment
where every request is blocked and the error appears in a customer's browser
console rather than in your logs. `*` is refused outright, and so is plain
`http`. Include each preview origin you actually want working — Vercel gives
every preview deployment its own.

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

## Vercel, specifically

- **Set the root directory to `web/`. This is not a preference.** Vercel
  treats a top-level `api/` directory as Serverless Functions, and this
  repository root has one containing the entire FastAPI application.
  `api/main.py` exports `app`, a real ASGI application, so Vercel's Python
  runtime will deploy and invoke it — and it dies on import, because
  `get_settings()` refuses to start without `JWT_SECRET` and `DATABASE_URL`.

  **The symptom is `500 FUNCTION_INVOCATION_FAILED` on every path**, with a
  Python traceback in the function logs ending in
  `ConfigError: JWT_SECRET is required but not set`. Nothing is wrong with
  the code at that point: the config module is doing exactly its job, in a
  place it was never meant to be deployed. Pointing the root directory at
  `web/` makes the `api/` directory invisible to Vercel and the problem
  disappears.
- **`NEXT_PUBLIC_API_BASE_URL` is a build-time value**, inlined into the
  bundle by `next.config.ts`. Changing it needs a redeploy, not an environment
  edit. Set it to the API's origin *including* the version prefix:
  `https://api.example.com/api/v1`.
- Nothing else. There are no route handlers, no middleware and no server-side
  fetching to configure.

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

1. **Supabase project**, then apply the migrations and `db/roles.sql`. Turn on
   PITR and test a restore — a backup you have not restored is a belief, not a
   backup.
2. **Redis**, managed.
3. **Object storage**, and the `S3ArtifactStore` that goes with it — see the
   blocker above. This is the one step that is code rather than an account.
4. **API and workers** to the container host, with `CORS_ORIGINS` pointing at
   the domain you are about to use. Check `/api/v1/health`.
5. **`web/` to Vercel**, root directory `web/`, pointed at the API.
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
