# 10 — Decisions

## Taken

| # | Decision | Why | Cost of reversing |
| --- | --- | --- | --- |
| 1 | Supabase for MVP (Postgres, auth, storage, RLS) | Collapses four components into one managed piece; schema is plain Postgres | Low — DDL ports unchanged; auth and storage adapters are thin |
| 2 | `org_id` on every tenant table | One-predicate RLS; missing filters are visible | Very high if deferred |
| 3 | Observations append-only, never updated | The trend line is the product | Very high — the history cannot be reconstructed |
| 4 | Deterministic detection, generative explanation | Verifiable, stable, cheap, cacheable | Medium |
| 5 | Postgres frontier with `SKIP LOCKED`, no Redis | Resumability for free; one fewer component | Low |
| 6 | Tokens in a separate `secrets` schema, envelope-encrypted | Client-role compromise must not reach Google accounts | Low |
| 7 | GSC unsliced totals stored separately from query rows | Anonymised-query gap is explained, not discovered | Medium |
| 8 | Per-site expected-CTR curve | Raw low-CTR flagging is a false-positive machine | Low |
| 9 | `scoring_version`, with historical recompute on change | A silently-moving score destroys trust | Low now, high later |
| 10 | Google connection is wizard step 2, not step 4 | 16-month backfill is the activation moment | Low |
| 11 | Hub as a bounded module with its own contract | Makes "standalone product" a packaging choice, not a rewrite | Medium |
| 12 | No Authority score until backlink data is licensed | A number with no source is a fabrication | None |
| 13 | AI visibility as a *mention rate* over N runs, never a boolean | LLM answers are non-deterministic; a boolean flickers | Low |
| 14 | Strategist uses typed tools, never model-written SQL | One injection away from a cross-tenant leak otherwise | Medium |
| 15 | Two runtimes (Next.js + FastAPI), boundary is the HTTP API | Crawl/analysis is Python-shaped; product surface is React-shaped | High |

## Open — needing your call

**1. Backlink data vendor, and therefore v2 pricing.** DataForSEO is cheapest
serious; Ahrefs/Majestic/Moz are direct. The per-lookup cost sets the Authority
module's COGS and the Professional tier's floor. *Get quotes during M2.*

**2. Business Profile in scope for v3?** The API needs a separate approval with
its own eligibility bar, and it is the only write-capable scope in the plan. It
is a genuinely strong differentiator for local businesses — the church example
is exactly the customer for whom the Maps pack *is* their visibility. Worth
applying early if yes, because approval is slow.

**3. Rank tracking against live SERPs — ever?** GSC covers the user's own site
honestly and free. SERP tracking adds competitor positions and non-GSC
keywords, at real cost and real terms-of-service exposure. It is the single
biggest "are we that kind of company" decision in the plan.

**4. Region and data residency.** EU customers will ask. Choosing the Supabase
region now is free; splitting regions later is not.

**5. Free-tier page cap.** 100 pages keeps crawl costs near zero but leaves
most real sites truncated, and a truncated audit undersells the product. 250
with a weekly-only schedule may convert better. Worth measuring rather than
guessing.

**6. Who owns the generated content?** Terms need to be explicit about drafts
produced by the content engine before v3 ships it.

## Things deliberately not done

- No auto-publishing path for generated content, at any tier.
- No write access to customer sites in v1; the `[Fix]` button waits for v3,
  with diff preview, one-click revert and post-change verification.
- No `site:` queries or SERP scraping for competitor page counts — the
  competitor's own sitemap is legitimate and more accurate.
- No modelled traffic figures for third-party domains presented as fact.
- No robots.txt override flag in the crawler, because an override flag
  eventually gets used.
