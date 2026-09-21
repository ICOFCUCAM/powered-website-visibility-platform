# Website Visibility Platform

An AI-powered website visibility platform. It does not stop at telling an owner
what is wrong with their site — it closes the loop:

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
| [08-structure.md](docs/08-structure.md) | Repository layout, services, deployment |
| [09-mvp-sequence.md](docs/09-mvp-sequence.md) | Build order with acceptance criteria |
| [10-decisions.md](docs/10-decisions.md) | Decisions taken, and the ones still open |

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
```

Each test exercises a claim the design depends on rather than the ORM's ability
to insert a row. The position test is the clearest example: the same data reads
as 3.17 when weighted by impressions and 11.50 when averaged naively, and the
naive figure is the one that looks plausible in a dashboard.

## Three rules the whole design hangs on

1. **Detection is deterministic; the LLM explains.** Rules find issues and
   produce stable fingerprints. The model writes the prose and the fix. It never
   decides what is broken and never invents a number.
2. **Observations are append-only.** A ranking, a crawl result and a score are
   never updated in place. The trend line is the product.
3. **Nothing ships a metric it cannot defend.** No fabricated search volumes, no
   modelled traffic presented as fact, no binary "yes" for a non-deterministic
   measurement.
