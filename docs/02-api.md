# 02 — API

FastAPI, versioned under `/v1`. JSON everywhere. The Next.js app never talks to
Postgres directly; every read goes through here so tenancy, plan limits and
metering are enforced in exactly one place.

## Auth

Bearer JWT from the identity provider. Middleware resolves `user_id`, loads
org memberships, and binds the org scope for the request. Handlers never accept
an `org_id` from the client — it is derived from the authenticated session and
the requested resource, then checked. Database connections additionally set
`app.user_id` so RLS applies as defence in depth even if a handler forgets a
filter.

Roles: `owner`, `admin`, `member`, `viewer`. Writes require `member` or above;
billing and connection-deletion require `admin`.

## Error contract

```json
{
  "error": {
    "code": "google_needs_reauth",
    "message": "Your Google connection expired. Reconnect to resume syncing.",
    "details": { "account_email": "user@example.com", "service": "search_console" },
    "retriable": false
  }
}
```

`code` is a stable machine string the UI branches on. `message` is shown to the
user as-is, so it is written for a non-technical reader. Codes that matter:
`google_needs_reauth`, `google_no_property_match`, `google_insufficient_permission`,
`plan_limit_exceeded`, `crawl_in_progress`, `ai_budget_exhausted`,
`site_not_verified`.

## Endpoints

### Sites

```
POST   /v1/sites                      {domain} → normalises, dedupes, creates
GET    /v1/sites
GET    /v1/sites/{id}                 includes onboarding_state, latest score
PATCH  /v1/sites/{id}                 display_name, timezone, crawl_schedule
DELETE /v1/sites/{id}                 soft delete (archived_at)
GET    /v1/sites/{id}/overview        the Home screen payload, one call
```

`/overview` returns the dashboard in a single request: current score plus
delta, attention counts by severity, the top recommendation, 28-day
performance with comparison, and recent changes. The home screen must not
assemble itself from nine round trips.

### Google Hub

```
GET    /v1/hub/connections                     per account, per service, status
GET    /v1/google/oauth/start                  ?site_id=&services=  → 302
GET    /v1/google/oauth/callback                → 302 back into the wizard
DELETE /v1/hub/accounts/{id}                   revokes with Google, purges vault
POST   /v1/hub/accounts/{id}/rediscover
GET    /v1/hub/resources?service=&site_id=     includes auto-match proposals
POST   /v1/hub/links                           {site_id, resource_id, service}
DELETE /v1/hub/links/{id}
POST   /v1/hub/links/{id}/backfill             idempotent; returns existing run
GET    /v1/hub/sync-runs?site_id=&service=
GET    /v1/sites/{id}/ga4/events               candidate GA4 events to map
POST   /v1/sites/{id}/ga4/goals                {event_name, label, goal_kind}
```

### Search performance

```
GET /v1/sites/{id}/performance?from=&to=&compare=
GET /v1/sites/{id}/queries?from=&to=&order_by=&limit=&cursor=
GET /v1/sites/{id}/pages?from=&to=&order_by=&limit=&cursor=
GET /v1/sites/{id}/queries/{hash}/history?from=&to=
GET /v1/sites/{id}/anonymised-share?from=&to=
```

Every response carrying query- or page-sliced totals also carries
`anonymised_clicks` for the window, so the UI can explain the gap against the
site totals instead of leaving the user to find it.

### Crawls and pages

```
POST   /v1/sites/{id}/crawls           {trigger:"manual"} → 409 if one is running
GET    /v1/sites/{id}/crawls
GET    /v1/crawls/{id}                 live progress: discovered/fetched/rendered
DELETE /v1/crawls/{id}                 cancel
GET    /v1/sites/{id}/pages/{page_id}  latest snapshot + issues + GSC history
GET    /v1/sites/{id}/pages/{page_id}/history
```

### Issues, plan, recommendations

```
GET    /v1/sites/{id}/issues?status=&category=&severity=&cursor=
GET    /v1/issues/{id}                 evidence, explanation, observation history
POST   /v1/issues/{id}/dismiss         {reason}
POST   /v1/issues/{id}/snooze          {until}
POST   /v1/issues/{id}/mark-applied    queues a verification crawl
GET    /v1/sites/{id}/plan/current
POST   /v1/sites/{id}/plan/regenerate  rate-limited, budget-checked
PATCH  /v1/recommendations/{id}        {status}
```

`mark-applied` is the hinge of the loop: it moves the issue to `applied`, writes
a `fix_actions` row, and enqueues a single-URL verification crawl that flips it
to `verified` or `regressed`. Nothing else in the API changes an issue's truth.

### Keywords, scores, reports

```
GET    /v1/sites/{id}/keywords?tracked=
POST   /v1/sites/{id}/keywords         {phrases[], source}
POST   /v1/sites/{id}/keywords/suggest {business_description}
DELETE /v1/sites/{id}/keywords/{kid}
GET    /v1/sites/{id}/score?from=&to=  history under one scoring_version
GET    /v1/sites/{id}/reports
POST   /v1/sites/{id}/reports/{rid}/send
GET    /v1/reports/{rid}/html          signed, expiring URL
```

### Strategist

```
POST   /v1/sites/{id}/strategist/messages   {message, conversation_id?}  (SSE)
GET    /v1/sites/{id}/strategist/conversations
```

Streams tokens and tool-call progress ("checking your top queries…") so a
multi-second answer does not look like a hang.

### Internal

Not exposed publicly; service-token auth, separate router:

```
POST /internal/jobs/crawl/{crawl_id}/lease
POST /internal/jobs/sync/dispatch
POST /internal/partitions/ensure
```

## Conventions

- Cursor pagination everywhere (opaque cursor, `limit` ≤ 200). Offsets over
  partitioned fact tables get slow exactly when a customer's data gets
  interesting.
- Dates are `YYYY-MM-DD` in the **site's** timezone; timestamps are UTC ISO-8601.
- `POST` endpoints that start work are idempotent within a window and return
  the in-flight resource rather than creating a duplicate.
- Rate limits per org, returned in headers, surfaced as `plan_limit_exceeded`
  with the limit and reset in `details`.
