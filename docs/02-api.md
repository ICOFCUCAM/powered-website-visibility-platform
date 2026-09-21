# 02 — API

FastAPI, versioned under `/v1`. JSON everywhere. The Next.js app never talks to
Postgres directly; every read goes through here so tenancy, plan limits and
metering are enforced in exactly one place.

## Auth

Bearer JWT from the identity provider. Middleware resolves `user_id`, loads
org memberships, and binds the org scope for the request. Handlers never accept
an `organization_id` from the client — it is derived from the authenticated session and
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
`site_not_verified`, `email_not_configured`, `invalid_status`,
`assistant_unavailable`.

## Endpoints

Base URL `/api/v1`, per the V1 spec (§30).

### Auth
```
POST /auth/register
POST /auth/login
POST /auth/logout
GET  /auth/me
```

### Websites
```
POST   /websites                 {url} → normalise, validate, dedupe, create
GET    /websites
GET    /websites/{id}
DELETE /websites/{id}
POST   /websites/{id}/crawl      409 if running; 403 unless crawl_allowed()
POST   /websites/{id}/verify     ownership check (GSC linkage, or DNS/file token)
```

### Google
```
GET    /google/connect           ?website_id=&services=  → 302 to Google
GET    /google/callback          → 302 back into the wizard
GET    /google/connections
DELETE /google/connections/{id}  revokes with Google, purges the vault row
```

### Properties
```
GET  /google/search-console/properties        includes auto-match proposals
POST /google/search-console/properties/{id}/connect
GET  /google/analytics/properties
POST /google/analytics/properties/{id}/connect
GET  /websites/{id}/analytics/events          candidate GA4 key events
POST /websites/{id}/analytics/goals           {event_name, label, goal_kind}
```

### Dashboard
```
GET /websites/{id}/dashboard
```

One call returns the whole Home screen: Visibility Health score with its
components and month-over-month delta, Google Search totals, Analytics totals,
and the top opportunities. The home screen must not assemble itself from nine
round trips.

### Search performance
```
GET /websites/{id}/search-performance   ?from=&to=&compare=&country=&device=
GET /websites/{id}/queries              ?from=&to=&order_by=&limit=&cursor=
GET /websites/{id}/pages                ?from=&to=&order_by=&limit=&cursor=
GET /websites/{id}/queries/{hash}/history
```

Every response that slices by query or page also carries `anonymised_clicks`
for the window, so the UI can explain the gap against site totals rather than
leaving the user to discover it.

### Analytics
```
GET /websites/{id}/analytics            ?from=&to=&dimension=
GET /websites/{id}/analytics/landing-pages
```

### Audit
```
GET  /websites/{id}/audit               counts by severity + grouped issues
GET  /websites/{id}/audit/{issue_id}    evidence, explanation, affected pages
POST /websites/{id}/audit/{issue_id}/resolve
```

`resolve` is the hinge of the loop: it sets the issue to `applied`, writes an
`actions` row, and enqueues a single-URL verification crawl that flips it to
`verified` or `regressed`. Nothing else in the API changes an issue's truth.

### AI
```
POST   /websites/{id}/ai/chat                    (SSE)
GET    /websites/{id}/conversations              history + suggested questions
GET    /websites/{id}/conversations/{cid}        messages, with their tool steps
DELETE /websites/{id}/conversations/{cid}
GET  /websites/{id}/plan                         this week's plan + priorities
GET  /websites/{id}/recommendations              ?status=
POST /websites/{id}/recommendations/{rid}/dismiss
POST /websites/{id}/recommendations/{rid}/complete
```

`complete` records INTENT, not evidence. It sets the recommendation to
`RESOLVED` and leaves the issue behind it open until a crawl observes it gone —
which is what lets next week's plan say "you marked this done and it is still
there". `?status=` rejects a value outside the four statuses rather than
returning an empty list, because an empty list reads as "you have none".

Each recommendation carries `prose_source` (`template` or `model`) so the UI
can label generated wording as generated. The rank beside it is always code.

`ai/chat` is the one streaming endpoint. It emits `data:`-framed JSON events —
`conversation`, `step` (a tool about to run, with its input), `delta` (a
fragment of the answer), `done` (with the run's tool steps) and `error`. Two
consequences worth stating:

- **A mid-answer failure is an `error` EVENT, not a 500.** The status line went
  out with the first byte, so a browser that gets a stream which simply stops
  has nothing to show.
- **The handler opens its own database connection.** FastAPI closes a `yield`
  dependency before the response body is streamed, so a handler relying on the
  request connection would find it returned to the pool during the first tool
  call. It is still the RLS-bound request role, bound to the same user.

`assistant_unavailable` is returned as a 503 before the stream starts when no
model is configured. There is no template fallback for a conversation: a
scripted reply pretending to be an analyst is worse than an honest absence,
and the screen says so rather than answering.

### Reports and account
```
GET    /account                       what deleting it would take with it
DELETE /account                       deletes the account and its data
GET    /websites/{id}/reports
POST   /websites/{id}/reports          generate this week's report now
POST   /websites/{id}/reports/{rid}/send
GET    /reports/{rid}/html            signed, expiring URL
```

`DELETE /account` is immediate and irreversible, and requires the caller to
send their own email address as `confirm_email` — the difference between a
mis-click and a decision. It deletes the organisations where they are the last
owner, revokes their Google access, destroys the encrypted refresh tokens,
removes the fetched HTML from object storage and deletes the sign-in, then
returns counts and a receipt id. Where any of that could not be done — Google
unreachable, no identity provider configured — the response says so rather
than reporting a clean sweep. See [14-deletion.md](14-deletion.md).

`/reports/{rid}/html` is the one route with no session, because it is opened
from a mail client where there is neither a session nor an Authorization
header. It takes `expires` and `signature` — an HMAC over the report id and
the expiry, under a context string of its own so a signature minted for it can
never be replayed against another signing use of the same secret. A bad
signature, an expired one and a report that does not exist all return the same
`not_found`; a distinct 404 would confirm which report ids are real.

`send` returns `email_not_configured` when no mail server is set up rather
than recording a delivery that did not happen. A failed delivery sets the
report to `failed` with its reason, so a report that did not arrive looks
different from one that did.

### Internal
Service-token auth, separate router, never publicly routed:
```
POST /internal/jobs/crawl/{crawl_id}/lease
POST /internal/jobs/sync/dispatch
POST /internal/partitions/ensure
```

## Conventions

- Cursor pagination everywhere (opaque cursor, `limit` ≤ 200). Offsets over
  partitioned fact tables get slow exactly when a customer's data gets
  interesting.
- Dates are `YYYY-MM-DD` in the **website's** timezone; timestamps are UTC ISO-8601.
- `POST` endpoints that start work are idempotent within a window and return
  the in-flight resource rather than creating a duplicate.
- Rate limits per org, returned in headers, surfaced as `plan_limit_exceeded`
  with the limit and reset in `details`.
