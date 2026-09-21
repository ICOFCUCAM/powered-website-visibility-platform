# Website Visibility Platform

An AI-powered website visibility platform. It does not stop at telling an owner
what is wrong with their website — it closes the loop:

```
Discover → Diagnose → Recommend → Fix → Measure → Repeat
```

## Status

Specification stage. No application code yet. The technical specification in
[`docs/`](docs/) is the source of truth; `db/migrations/` holds the schema it
describes.

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
