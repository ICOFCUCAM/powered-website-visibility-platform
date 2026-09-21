# 04 — Google Hub

The Hub's job, stated precisely:

> Hide Google's **technical complexity** from the customer while keeping
> Google's **authorisation and consent** fully intact.

Those are different things. The first is the product. The second is
non-negotiable: there is no legitimate way to create, access or bypass a user's
Google services on their behalf, and any design that appears to do so is a
design that gets the OAuth client banned.

What the user sees:

```
1. Enter your website        www.example.com
2. Connect Google            [ Continue with Google ]
3. Choose your website       ☑ Search Console  ☑ Analytics  ☐ Business Profile
4. Start analysis
```

What happens behind it: OAuth 2.0 authorisation code flow with PKCE, token
vaulting, multi-service resource discovery, host matching, backfill scheduling,
and quota-aware incremental sync.

## 0. Start OAuth verification before writing code

This is the longest-lead item in the entire project and it is free to begin.

`webmasters.readonly` and `analytics.readonly` are **sensitive** scopes. An
unverified OAuth client is capped at 100 test users and shows an unnerving
"Google hasn't verified this app" interstitial. Verification requires a
published privacy policy on the verified domain that owns the OAuth client, a
homepage explaining the app, an accurate scope justification, and a demo video
walking through the consent flow. Review commonly takes several weeks and
bounces at least once on wording.

Business Profile is stricter still: the API is not open by default. Access is
requested through a separate application with its own eligibility criteria, and
approval is neither quick nor guaranteed. **Therefore GBP is never a blocking
step in the wizard** — it renders as an optional card and, until approval
lands, as "Coming soon" rather than a dead button.

Practical sequencing: register the Cloud project, publish the privacy policy,
and submit verification during the first week of development, in parallel with
building against test users.

## 1. Scopes, requested incrementally

| Service | Scope | When requested |
| --- | --- | --- |
| Identity | `openid`, `email`, `profile` | Step 2, always |
| Search Console | `https://www.googleapis.com/auth/webmasters.readonly` | Step 2, with identity |
| Analytics | `https://www.googleapis.com/auth/analytics.readonly` | Step 3, only if the user opts in |
| Business Profile | `https://www.googleapis.com/auth/business.manage` | v3, on demand, after API approval |
| Ads | `https://www.googleapis.com/auth/adwords` | v3, on demand |

Request the minimum first and widen on demand (`include_granted_scopes=true`).
Asking for five scopes on the first screen depresses consent conversion and
widens the verification surface. Read-only everywhere; `business.manage` is the
only write-capable scope and it arrives only when the user asks for posting and
review features.

The granted set can be narrower than the requested set — Google lets users
uncheck individual permissions. Always reconcile against the `scope` field in
the token response, store it in `connections.granted_scopes`, and gate
features on what was actually granted, never on what was asked for.

## 2. The flow

```
Browser                    API                        Google
   |  GET /v1/google/oauth/start?website_id=&services=
   |------------------------------------->|
   |                                       | build state (signed, 10-min TTL,
   |                                       |   binds organization_id + website_id + nonce)
   |                                       | build PKCE verifier/challenge
   |  302 to accounts.google.com ----------|
   |------------------------------------------------------>|
   |                          user consents                 |
   |  302 /v1/google/oauth/callback?code=&state=            |
   |<-------------------------------------------------------|
   |------------------------------------->|
   |                                       | verify state + PKCE
   |                                       | exchange code -> tokens
   |                                       | id_token -> google_sub, email
   |                                       | encrypt refresh token -> vault
   |                                       | upsert connections
   |                                       | enqueue discovery job
   |  302 /onboarding/choose?account=      |
```

Rules that keep this safe:

- `access_type=offline`, `prompt=consent` **only** when no refresh token is
  held for that `google_sub`. Forcing the consent screen on every connect is a
  common bug that re-prompts returning users for no reason.
- `state` is a signed, short-lived token binding org, website and nonce. Never a
  raw UUID, never reusable.
- Key the account on `google_sub`, never on email — users change their email
  and you would silently fork one account into two.
- The refresh token is encrypted before it touches the database. Access tokens
  are held in memory and in a short-TTL cache only; they are never persisted.
- Revocation is a first-class path: `invalid_grant` on refresh sets
  `status='needs_reauth'`, surfaces a re-connect card in the Hub screen, and
  pauses sync jobs rather than retrying into a lockout.

## 3. Discovery

One job, immediately after consent, fanning out across every granted service:

| Service | Call | Yields |
| --- | --- | --- |
| Search Console | `websites.list` | property URIs + permission level |
| Analytics | `accountSummaries.list` | account → property tree |
| Business Profile | `accounts.list` → `locations.list` | locations |
| Ads | `customers.listAccessibleCustomers` | customer ids |

Everything lands in `connection_properties` with `matched_hosts` normalised:

| Resource | `matched_hosts` |
| --- | --- |
| `sc-domain:example.com` | `{example.com}` — covers every subdomain and scheme |
| `https://www.example.com/` | `{www.example.com}` |
| GA4 `properties/123` | from the property's configured stream URLs |

Auto-match proposes a link when the resource's hosts intersect the website's
registrable domain. Prefer a domain property over a URL-prefix property when
both exist — it has complete coverage. Present the proposal pre-ticked and
always overridable; `website_connections.link_method` records `auto` vs
`user_selected` so a wrong match is diagnosable months later.

### Failure states the wizard must handle

These are the majority of real support tickets, so they are specified, not
left to the happy path:

| Situation | What the wizard does |
| --- | --- |
| No GSC property matches the domain | Explain that Google needs to verify ownership first; link the verification docs; offer to continue with crawl-only analysis and reconnect later |
| Property exists but permission is `siteUnverifiedUser` | Data is unavailable; tell the user which Google account owns it and offer to switch account |
| Several matching properties | List them with permission level and coverage; pre-tick the domain property |
| User signed in with the wrong Google account | Show the connected email prominently with a one-click "Use a different account" that adds a second `connections` row rather than replacing the first |
| Analytics granted, no GA4 property | Note that Universal Analytics is not supported; continue without it |
| GA4 property found, no goal event chosen | Blocking step **for outcome reporting only** — traffic data still flows; the dashboard shows "outcomes not configured" rather than a fabricated conversion rate |
| GBP not yet approved for the platform | Card renders "Coming soon"; never a button that fails |

## 4. Sync

**Backfill on connect, immediately.** 16 months of GSC daily rows, oldest
first, so the trend chart populates while the user is still in the wizard. This
is the activation moment of the entire product: a brand-new account sees real
history within minutes, before the crawler has finished a single page.

**Incremental daily** thereafter, re-fetching a trailing window rather than
only yesterday — Google restates recent days, and data lags 2–3 days. Re-fetch
`[today-5, today-3]` each night and upsert. Never display today.

| Dataset | Dimensions | Window | Cadence |
| --- | --- | --- | --- |
| `gsc_daily_totals` | none | 16 months | daily |
| `gsc_query_daily` | date, query, country, device | 16 months | daily |
| `gsc_page_daily` | date, page, country, device | 16 months | daily |
| `gsc_query_page_daily` | date, query, page | trailing 90 days, top 1,000 queries | weekly |
| `ga4_daily` / `ga4_page_daily` | date, channel / page | 14 months | daily |

Quota discipline: paginate at 25,000 rows, respect per-minute and per-day
limits per property, back off exponentially on 429, and record `quota_hits` on
`sync_runs` so throttling is visible rather than mysterious. Backfills
run at low priority on their own worker pool so one new agency account cannot
starve every existing tenant's nightly sync.

Every run writes a `sync_runs` row. A partial sync is recorded as
`partial` with its range, so gaps in a chart are explainable and re-runnable
instead of permanent.

## 5. Consent transparency

Each connector card states, in the user's language and before the redirect,
exactly what will be read:

> **Google Search Console** — we read your search performance: queries, clicks,
> impressions, positions and which pages appear in Google. We never change
> anything in your Search Console account.

Disconnect must actually disconnect: revoke the token with Google, delete the
vault row, mark the account revoked, and tell the user plainly what happens to
already-synced data (retained, or purged on request). Anything less is a
compliance problem as well as a trust problem.

## 6. Standing alone

The Hub is built as its own module (`api/hub/`) with a hard boundary: it may
not import crawler, scoring or recommendation code, and the analysis side may
read only the normalised tables in `0002`/`0003`, never a Google client. The
Hub's public contract is:

- `GET /v1/hub/connections` — what is connected, per service, per account
- `GET /v1/hub/resources?service=` — everything discovered
- `POST /v1/hub/links` — attach a resource to a website
- `GET /v1/hub/data/search-analytics` — normalised, already-correct rollups
- webhook `hub.sync.completed` — fired when a backfill or daily sync lands

Keeping that boundary costs almost nothing now and is what makes "Google Hub as
a product in its own right" a packaging decision later rather than a rewrite.
