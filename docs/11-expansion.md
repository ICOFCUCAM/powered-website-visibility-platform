# 11 — Expansion seams

The V1 spec (§42) lists eleven things not to build. Each is out of scope and
has no V1 surface. This document is the answer to the other half of the
question: **when we do build them, what already exists, and what has to change?**

The rule applied throughout: a seam earns its place only if adding the feature
later would otherwise mean rewriting something that already works. Everything
else was left out.

These seams exist to serve a boundary, not to invite scope creep:

| | | |
| --- | --- | --- |
| **V1** | Understand | connect → collect → crawl → diagnose → score → recommend → report |
| **V2** | Act | approve → execute → preserve before-state → verify → measure |
| **V3** | Expand | more surfaces, more providers, local / search / AI intelligence |

> If a feature does not improve *Understand*, it waits.

## The five seams

```
                    ┌───────────────────────┐
                    │    Visibility Hub     │
                    │         core          │
                    └───────────┬───────────┘
                                │
    ┌───────────┬───────────┬───┴───────┬────────────┐
    ▼           ▼           ▼           ▼            ▼
 connection   action      data      surfaces &    crawl
   layer      ledger    providers    scoring      corpus
```

| Seam | Table(s) | What plugs in |
| --- | --- | --- |
| Connection layer | `connections`, `connection_properties`, `providers` | Business Profile, Ads, WordPress, Bing, Meta |
| Action ledger | `actions`, `action_capabilities` | GBP management, WordPress auto-edit, Ads management, automated publishing |
| Data providers | `data_providers`, `external_metrics`, `backlinks`, `referring_domains` | Backlink intelligence, keyword volume, competitor traffic, benchmarks |
| Surfaces & scoring | `visibility_surfaces`, `score_components` | AI-search visibility, Maps, Social, Paid |
| Crawl corpus | `crawls.corpus`, `page_snapshots.extract_version` | Competitor crawling, web-scale index |

## The eleven, mapped

| Not in V1 (§42) | Seam | Already exists | Still needed |
| --- | --- | --- | --- |
| Google Ads management | connection + action | `provider_services` row, `ads.budget.update` capability marked `destructive` | Ads API client, approval UI, spend guardrails |
| Business Profile management | connection + action | `business_profile` service row, three GBP capabilities, `connection_properties.geo`, `maps` surface | **GBP API approval** (separate application, slow), client, post/review UI — sequenced so it cannot block the rest of Phase 3 |
| Automated backlink acquisition | — | nothing, deliberately | Nothing. Outreach *suggestions* fit the data-provider seam; automated acquisition is not a product this platform should have |
| Proprietary internet-wide traffic estimates | data providers + corpus | `external_metrics.is_modelled`, `index` corpus | A corpus, a model, and an honest confidence interval |
| Massive web crawler | crawl corpus | `corpus` column, versioned extraction documents in object storage, per-host politeness budget | A different store (columnar, not Postgres), distributed frontier, far larger politeness infrastructure |
| Social-media analytics | connection + surfaces | `meta` provider, `social` surface, dimension-style fact tables | Platform clients, a social score component |
| Automated publishing | action + content | `content_drafts` with a review state, `wp.post.publish` capability | Publisher clients. **No status bypasses the approval gate** |
| WordPress auto-editing | connection + action | `wordpress` provider, `page.meta.update` and `page.image.compress` marked reversible and standing-approval-eligible | Plugin or application-password flow, diff preview UI, revert path |
| Competitor traffic estimation | data providers | `competitors`, `external_metrics` with provenance | A vendor, or an own model, and labelling as modelled |
| Enterprise white-labeling | tenancy | `organizations.parent_organization_id`, `organization_branding`, `audit_log` | Custom domain routing, report theming, SSO |
| Mobile apps | API | versioned `/api/v1` that the web app itself consumes, `api_keys` | Clients, push infrastructure |

Two entries there are worth reading twice. **Automated backlink acquisition**
has no seam because it should not be built: the product's credibility rests on
never claiming to control Google's ranking, and link acquisition automation
contradicts that directly. And **massive web crawler** has the thinnest seam of
the eleven — the versioned extraction document means a future index can be built
by replaying extraction over a different corpus, but the store, the frontier and
the politeness infrastructure are all genuinely new work. That is the correct
conclusion: it is a different company, not a feature.

## Business Profile is sequenced, not scheduled

Its API approval is a separate application with its own eligibility bar, and
neither the timing nor the outcome is ours to control. So it is decomposed, and
only the first step is ours to do now:

```
Phase 3
 ├── Business Profile
 │     ├── application submitted   ← start now, it is the long pole
 │     ├── approval                ← external, unknown duration
 │     └── implementation          ← begins when approval lands
 │
 └── every other Phase 3 capability
       AI-search visibility · WordPress · alerts · agency dashboards
       ↑ none of these depend on GBP approval
```

An external approval delay must not idle unrelated engineering. Until approval
lands, GBP exists only as the generic connection and property seams that are
already there — a `provider_services` row and three disabled capabilities. It
gets **no dedicated V1 Hub UI and no bespoke data model**; the Hub screen
renders it as "coming soon" rather than a button that fails.

## What makes empty tables safe

A table existing must never cause the UI to render a zero. Every surface, score
component and provider-backed panel is gated on its catalogue row being
`enabled` **and** on data being present.

Authority is the working example. `score_components` carries it at weight 0.00
with `enabled = false` until a backlink vendor is contracted, so the scorecard
**omits** it. It never shows "Authority 0", which would be a fabricated
measurement of a real thing. `db/tests/smoke.sql` test 7 asserts that the
enabled weights still total exactly 1.00 with components disabled.

## The one-way doors

Three decisions here cannot be deferred cheaply, which is why they are in the
schema before any of these features exist:

1. **Organisation hierarchy.** `parent_organization_id` is nullable and unused
   in V1. Adding it later means revisiting every permission check in the
   product.
2. **The audit log.** It cannot be backfilled. The first enterprise customer
   will ask for history that does not exist.
3. **Before-state on every action.** The check constraint
   `applied_actions_are_revertible` means no action can reach `applied` without
   a recorded prior state. Every future write capability inherits revertibility
   by construction rather than by developer discipline — proven by smoke test 6.
