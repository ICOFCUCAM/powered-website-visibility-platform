# 00 — Overview

## The promise

> Don't just understand your website. Improve its visibility.

Competitors sell a number. This platform sells a loop:

| Stage | What the user sees |
| --- | --- |
| Discover | Crawl + Google Search Console connection |
| Diagnose | Issues in plain language, ranked by measured impact |
| Recommend | "What should I do this week?" — four things, not a hundred |
| Fix | Instructions, or an applied change where it is safe and reversible |
| Measure | Re-crawl verification, and the search data that followed |
| Repeat | Weekly |

Everything below serves that loop. A feature that cannot be verified after the
fact does not belong in the MVP.

## Two products, one platform

```
                    PLATFORM
                        |
           +------------+------------+
           |                         |
      GOOGLE HUB               VISIBILITY AI
           |                         |
   Search Console                    |
   Analytics                         |
   Business Profile (v3)             |
   Ads (v3)                          |
           |                         |
           +------------+------------+
                        v
                  UNIFIED DATA
                        v
                   AI ANALYSIS
                        v
                   ACTION PLAN
```

**Google Hub** absorbs the complexity of Google's authorisation and API
surface and exposes a normalised data layer. **Visibility AI** consumes that
layer alongside the crawler. They share a database but not a codebase
boundary: the Hub never imports analysis code, and the analysis code reads the
Hub only through its normalised tables. This keeps the Hub shippable on its own.

The positioning sentence, which the whole product should be able to keep:

> Connect your website once. We bring your Google data together, explain what
> it means, and tell you what to do next.

## Scorecard

The user-facing score has five components. Each is computed from stored,
inspectable sub-metrics under a named `scoring_version`.

| Component | Source | In MVP |
| --- | --- | --- |
| Technical SEO | Crawl extraction + CrUX/PSI sampled field data | yes |
| Google visibility | GSC impressions, clicks, CTR vs position baseline | yes |
| Content | Crawl extraction (depth, uniqueness, structure, schema) | yes |
| Authority | Licensed backlink data | no — v2 |
| AI visibility | Computable proxies in MVP; sampled live measurement in v3 | partial |

A component with no data source is absent, not zero. Showing `Authority: 48`
before backlink data is licensed would be an invented number.

## The V1 → V2 → V3 boundary

Three words, and a rule that settles most scope arguments before they start.

### V1 — Understand

```
connect → collect → crawl → diagnose → score → recommend → report
```

### V2 — Act

```
approve → execute → preserve before-state → verify → measure
```

The new capability is the **action → verification loop**, not more dashboards:

```
issue → recommendation → user approves → action executed
      → before_state preserved → crawl or Google observation
      → did the condition actually improve?
```

That is worth considerably more than another report, and the substrate for it
(`actions`, `action_capabilities`, the before-state constraint) is already in
the V1 schema.

### V3 — Expand

Additional visibility surfaces, external data providers, and local / search /
AI intelligence — the point at which this stops being a technical SEO audit and
becomes a website visibility intelligence system.

### The rule

> **If a feature does not improve *Understand*, it waits.**

The architecture does not prevent it from arriving later. That is the whole
point of the seams in [11-expansion.md](11-expansion.md) — and the reason the
answer to "is this V1 or already V2?" is: *the product is V1; the architecture
deliberately contains seams for V2 and V3.*

## Version roadmap

**MVP (v1)** — the launchable slice.

```
Connect Google  →  Crawl  →  Analyse  →  Keywords  →  AI plan  →  Weekly report
```

Google connection comes *first*, not fourth. GSC backfills 16 months of history
in minutes, so a brand-new account sees a populated trend chart before the
crawler has finished. That is the activation moment; a dashboard reading "not
enough data yet" for two weeks is how the product loses people.

**v2 — competitive layer.** Competitor tracking, keyword gaps, backlink
monitoring (licensed), content opportunities, automated reports.

**v3 — differentiation.** AI-search visibility measurement, industry
benchmarks, content briefs, WordPress write-back, alerts, agency dashboards.

**v4 — data platform.** Proprietary traffic estimation, larger web index,
advertising intelligence, public API, enterprise accounts.

## Explicit non-goals for the MVP

- **No backlink or competitor metrics.** They require a licensed link index.
  Ship the module when the contract is signed, not before.
- **No SERP rank scraping.** GSC gives real position data for the user's own
  website, free and within terms. Rank tracking against live SERPs is a separate
  product with a different cost and risk posture.
- **No traffic estimates for third-party domains.** Modelled figures are
  routinely off by an order of magnitude.
- **No auto-publishing of generated content.** Drafts land in a review state and
  there is no code path that publishes without a human.
- **No write access to user websites in v1.** The `[Fix]` button ships in v3, with
  diff preview, one-click revert and post-change verification.

## Business model

| Tier | Price | Shape |
| --- | --- | --- |
| Free | £0 | 1 website, 100 crawled pages, weekly crawl, 10 tracked keywords |
| Professional | $29–$79/mo | 5 websites, 2,000 pages, daily GSC sync, AI consultant, full history |
| Agency | $149–$499/mo | 25+ websites, client dashboards, white-label reports, team seats |
| Enterprise | Custom | SSO, SLA, API, dedicated crawl capacity |

Page caps and AI budgets are enforced per organisation in `organizations.plan` and
metered through `llm_calls` (see [06-ai-layer.md](06-ai-layer.md)). One agency
user crawling forty websites must not cost more than their subscription.

## The Google Hub

The onboarding wizard that connects Search Console, Analytics and Business
Profile is specified as a **separately deployable module** with its own data
layer, because it is plausibly a product in its own right. See
[04-google-hub.md](04-google-hub.md).
