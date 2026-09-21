# Website Visibility Platform

An AI-powered website visibility platform. It does not stop at telling an owner
what is wrong with their website — it closes the loop:

```
Discover → Diagnose → Recommend → Fix → Measure → Repeat
```

## Status

**V1 is complete, it runs by itself, and a customer can end it themselves.** The database, the API, the Google
connection layer (OAuth with PKCE, encrypted token vault, property discovery,
auto-matching, linking, ownership), Search Console and GA4 synchronisation,
the crawler, the rules engine and scoring, the dashboard and audit screens,
the weekly plan and its email, the AI Strategist, and the wizard a customer
actually walks through:

```
1. Your website        example.com
2. Google account      you@example.com
3. Search Console      sc-domain:example.com   ← best match, pre-selected
4. Analytics           Example — GA4
   What counts as a result?  contact_form_submit
5. Business Profile    Coming soon
6. Website scan        16 months of search history + 14 of analytics
```

and the weekly report it produces:

```
example.com: 1,456 clicks, 4 things to fix

Clicks rose by 224 to 1,456 for the period.
Visibility score 52/100 · position 12.4, 5.6% better than the previous 28 days

1. Write search descriptions for 5 pages     about 252 more clicks a month
2. Refresh the page losing traffic           /sourdough
3. Push the search just below page one onto it
4. Serve 6 pages' text without JavaScript
```

and the Strategist, which answers from that data and nothing else:

```
> Why did my traffic change?
  checking your search performance…
  finding what changed…

  Your clicks rose by 224 to 1,456 between 22 Aug and 18 Sep, against the
  28 days before. Nothing in the query data moved far enough to explain it.
  The largest thing you could act on today is missing search descriptions
  on 5 pages — worth roughly 252 clicks a month.

  ▸ 3 checks against your data
```

All fourteen items in the V1 definition of done are met, including the two
that are usually still open at beta: **disconnect Google** — which revokes the
token rather than dropping our copy of it — and **delete your account**, which
reaches the eleven tables a cascade cannot, the encrypted refresh tokens, the
fetched HTML in object storage and the sign-in itself, then hands back a
receipt. See [14-deletion.md](docs/14-deletion.md).

Every night the schedule syncs Google, crawls, re-scores, rewrites the plan
and — on Mondays — builds the report, each website on its own minute so the
fleet does not arrive at Google's door together. A missed window is late, not
lost: the dispatcher claims the most recent slot that has passed, so a worker
pool that was down overnight catches up rather than skipping a day. See
[13-scheduler.md](docs/13-scheduler.md).

When something breaks, somebody is told — once. Four hundred failed syncs are
one alert, not four hundred; it is counted while it persists, repeated after
six hours so silence cannot read as recovery, and closed with a recovery
notice. The split is deliberate: "the machine is broken" goes to an operator
channel with no customer domain in it, and "your Google connection has stopped
working" goes to the customer, with what to do about it. Neither audience gets
the other's problems. See [15-alerting.md](docs/15-alerting.md).

The Strategist reads through nine typed, read-only tools. It writes no SQL and
cannot name a tenant: no tool schema contains an organisation or website id,
so scope comes from the session and there is no argument that could reach
another customer's data.

The AI layer runs **with or without a model** for explanations and the weekly
plan. Selection and ranking are code, so the four priorities in a plan are the same either way; a model writes the
prose when one is configured, and a validator refuses any generation
containing a figure the customer's own data does not support. With no
`ANTHROPIC_API_KEY` set, every explanation and plan comes from hand-written
templates drawn from the issue catalogue and the rule thresholds — plain,
correct, and made of the same numbers. The Strategist is the exception and
says so: a conversation has no honest template, so with no key configured the
endpoint returns `assistant_unavailable` and the screen explains rather than
answering.

The live Google handshake is the one thing untested here, because it needs a
verified Cloud project and a real user's consent. Everything up to it runs
against a fake Google that drives the real client code.

```
./scripts/dev-db.sh      # throwaway Postgres with every migration applied
eval "$(./scripts/dev-db.sh)"
export JWT_SECRET=<at least 32 bytes>
.venv/bin/uvicorn api.main:app --port 8000     # API
cd web && npm run dev                          # UI on :3000
./scripts/worker.sh beat                       # the nightly clock
./scripts/worker.sh all                        # every worker pool (dev)
./scripts/check.sh       # lint, import contracts, pytest, schema, typecheck
```

| | |
| --- | --- |
| `api/domain/` | Pure logic — URL normalisation, the crawl policy, models, errors. No framework, no vendor. |
| `api/adapters/` | The only place Supabase and the database driver appear. |
| `api/repositories/` | Postgres implementations of the domain's repository protocols. |
| `api/routers/` | `/api/v1` surface. |
| `api/hub/` | The Google Hub: the only module that may reach Google. Its own routes, services, providers and domain events. |
| `api/crawler/` | Politeness, frontier, fetch, extraction, render escalation. |
| `api/analysis/` | 27 deterministic rules, the CTR baseline, versioned scoring. |
| `api/ai/` | The only module that may reach a model. Versioned prompts, the evidence cache, budget admission, the numbers validator, the weekly plan, the Strategist loop. |
| `api/ai/tools/` | The nine typed, read-only queries the Strategist may run. Scope is bound server-side; no schema names a tenant. |
| `api/reports/` | Weekly report assembly, the HTML and text email, signed links, delivery. |
| `api/workers/` | The nightly schedule. Decides *when*; everything it runs is code the API already exercises. |
| `api/account/` | Ending an account. The one module that spans the Hub boundary, because deletion must revoke Google access *and* delete product data. |
| `web/` | Next.js: onboarding wizard, dashboard, audit, this week's plan, the assistant, sign-in. |
| `db/` | Migrations, roles, schema tests. |

The technical specification in [`docs/`](docs/) remains the source of truth.

## Documents

| Doc | Contents |
| --- | --- |
| [00-overview.md](docs/00-overview.md) | Product thesis, scope, version roadmap, non-goals |
| [01-data-model.md](docs/01-data-model.md) | Every table, why it is shaped that way, RLS, partitioning |
| [02-api.md](docs/02-api.md) | REST surface, auth, error contract |
| [03-crawler.md](docs/03-crawler.md) | Frontier, politeness, render escalation, storage |
| [04-google-hub.md](docs/04-google-hub.md) | OAuth, discovery, onboarding wizard, GSC/GA4/GBP |
| [05-analysis-scoring.md](docs/05-analysis-scoring.md) | Issue catalogue, fingerprints, scoring model |
| [06-ai-layer.md](docs/06-ai-layer.md) | Prompts, model routing, grounding rules, cost control |
| [07-ui.md](docs/07-ui.md) | Screen-by-screen specification |
| [08-architecture.md](docs/08-architecture.md) | **Frozen.** Repository layout, module boundaries, services, deployment |
| [09-mvp-sequence.md](docs/09-mvp-sequence.md) | **Frozen.** Build order with acceptance criteria |
| [10-decisions.md](docs/10-decisions.md) | **Frozen.** Locked decisions, and the ones still open |
| [11-expansion.md](docs/11-expansion.md) | Seams for everything deliberately not in V1 |
| [12-v1-conformance.md](docs/12-v1-conformance.md) | **Frozen.** Section-by-section against the V1 spec |
| [13-scheduler.md](docs/13-scheduler.md) | The nightly schedule: claims, leases, pools, what an operator reads |
| [14-deletion.md](docs/14-deletion.md) | Disconnect and account deletion: what the cascade misses, and how it is verified |
| [15-alerting.md](docs/15-alerting.md) | Operator incidents and customer notices: the line between noise and silence |

## V1 → V2 → V3

| | | |
| --- | --- | --- |
| **V1** | Understand | connect → collect → crawl → diagnose → score → recommend → report |
| **V2** | Act | approve → execute → preserve before-state → verify → measure |
| **V3** | Expand | more visibility surfaces, external providers, local and AI intelligence |

**If a feature does not improve *Understand*, it waits.** The product is V1;
the architecture deliberately contains seams for V2 and V3.

## Handoff status

```
ARCHITECTURE          FROZEN
DATABASE              VERIFIED
CONSTRAINTS           CI-ENFORCED
V1 SCOPE              FROZEN
V2/V3 SEAMS           DEFINED
GOOGLE VERIFICATION   TIME-SENSITIVE  → submit now, build against test users
GBP APPLICATION       TIME-SENSITIVE if Phase 3 depends on it
BACKLINK VENDOR       COMMERCIAL      → out of V1 entirely
crawl_allowed         DECIDED (M1)    → derived, never user-controlled,
                                        enforced before dispatch
```

No further architecture work unless implementation exposes a contradiction.

## Architectural source of truth

Four documents are frozen. Changing one is an architectural decision, recorded
with its reason, not an edit:

`08-architecture.md` · `09-mvp-sequence.md` · `10-decisions.md` · `12-v1-conformance.md`

## Verifying the schema

The migrations apply cleanly to Postgres 16 and the smoke tests pass:

```
$ DB=postgres://postgres@localhost/postgres ./db/tests/run.sh
PASS  tenant isolation: org A sees only example.com
PASS  weighted position 3.17 vs naive avg 11.50 (the naive figure is the bug)
PASS  anonymised gap surfaced: 60 clicks withheld by Google (60 percent)
PASS  frontier leased 2 of 3, and a dead lease was reclaimed
PASS  second active search_console link rejected
PASS  historical unlinked row still permitted
PASS  applied action without before_state rejected
PASS  applied action with before_state accepted
PASS  enabled score weights total 1.00 with 2 component(s) awaiting a data source
PASS  standing approval restricted to reversible capabilities
PASS  every derived table carries source, derived_from and a calculation version
PASS  score of 76 traces to source=derived, version=1.0.0, crawl=c0000000-…
PASS  worker-b claimed all 3 without worker-a recovering
PASS  property coverage correct across 11 cases (www, scheme, subdomain, suffix-spoof, path)
PASS  ownership requires a COVERING property held as owner, not mere access
PASS  reconciliation authority is stored, never derived from dimensional rows
PASS  a modelled estimate cannot be declared deterministic
PASS  a generation without provider or grounding evidence is rejected
PASS  model output records provider, version, prompt and its grounding evidence
PASS  provenance index separates reproducible from model-dependent rows
PASS  a client role cannot write crawl_allowed
PASS  a client role cannot raise its own plan limits
PASS  ordinary columns on the same table stay writable
```

Each test exercises a claim the design depends on rather than the ORM's ability
to insert a row. The position test is the clearest example: the same data reads
as 3.17 when weighted by impressions and 11.50 when averaged naively, and the
naive figure is the one that looks plausible in a dashboard.

## Five rules the whole design hangs on

1. **Google is never in the read path.** It is reached only by scheduled syncs
   that write to Postgres. Dashboards read Postgres. An outage degrades to
   stale data with an honest timestamp, never an error page.
2. **Only the Hub may construct or call a Google API client.** Enforced by a CI
   import contract, not by review. What crosses the boundary is normalised
   facts and internal domain events — never a client or a credential.
3. **Detection is deterministic; the model explains.** Rules find issues and
   emit stable fingerprints. The model writes the prose and the fix. *AI may
   explain and prioritise existing evidence; it may not create evidence.*
4. **Observational facts are append-only; operational state is updated
   normally.** Measurements, snapshots and scores are never overwritten —
   that history is the product. `users`, `websites` and status fields change
   like any other row. The three tiers are specified in
   [01-data-model.md](docs/01-data-model.md).
5. **Nothing ships a number it cannot defend.** Every derived value carries its
   source, the records it came from, and whether it is reproducible.
   Deterministic values (scores, rule findings, rankings) are reproducible from
   the same inputs and `calculation_version`. Model output is not, and does not
   pretend to be — it records provider, model version, prompt version, the
   evidence it was grounded in, and when. No fabricated search volumes, no
   modelled traffic presented as fact, no metric with no source shown as zero.
