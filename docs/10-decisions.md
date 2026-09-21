# 10 — Decisions

> **Frozen — architectural source of truth.**
> Changes to this document are architectural decisions, not edits. Amend it
> deliberately, and record the reason in the table below.

## Locked

These are settled. Changing one is a deliberate architectural decision with a
migration attached, not a preference.

| # | Decision | Why | Cost of reversing |
| --- | --- | --- | --- |
| 1 | **Supabase for the MVP**, with Supabase-specific code confined to `api/adapters/` | Collapses Postgres, Auth and Storage into one managed piece without letting the domain layer depend on it | Low, and kept low by the CI contract that forbids vendor imports in `api/domain/` |
| 2 | **The Hub is a bounded module.** It may know OAuth, provider accounts, properties, tokens, scopes, connection status and its own events; it may not import crawler, scoring, recommendation, dashboard or competitor code | Makes extraction to a standalone service a deployment change rather than a rewrite | Medium, and the boundary is CI-enforced so it cannot erode quietly |
| 3 | **The AI Strategist ships last (M9)** | The AI is an interpretation layer over accumulated evidence; built early it has nothing to interpret | None |
| 4 | `organization_id` on every tenant table | One-predicate RLS; a missing filter is a visible omission | Very high if deferred |
| 5 | Observations append-only, never updated | The trend line is the product | Very high — history cannot be reconstructed |
| 6 | Deterministic detection, generative explanation | Verifiable, stable, cacheable | Medium |
| 7 | Postgres frontier with `SKIP LOCKED`, no Redis in the MVP | Resumability by construction; one fewer component | Low |
| 8 | Tokens in a separate `secrets` schema, envelope-encrypted, no policies, no grants | A compromised client key must not reach a customer's Google account | Low |
| 9 | GSC unsliced totals stored separately from query rows | The anonymised-query gap is explained, not discovered | Medium |
| 10 | Per-website expected-CTR curve | Raw low-CTR flagging is a false-positive machine | Low |
| 11 | `scoring_version`, with historical recompute on change | A silently-moving score destroys trust | Low now, high later |
| 12 | Google connection is wizard step 2 | The 16-month backfill is the activation moment | Low |
| 13 | **The application owns the third-party data model; vendors own acquisition** | Backlinks, keyword volume and traffic estimates are modelled in domain terms, behind a provider interface | Low — swapping vendor touches one adapter |
| 14 | Provider-agnostic connection layer (`connections`, not `google_accounts`) | Business Profile, Ads, WordPress and social all arrive as connected accounts | Low now, high after launch |
| 15 | Write capabilities go through one action ledger with approval, before-state and revert | Four future features share the substrate; approval and audit cannot be retrofitted safely | High if deferred |
| 16 | AI visibility as a *mention rate* over N runs, never a boolean | LLM answers are non-deterministic; a boolean flickers | Low |
| 17 | Strategist uses typed read-only tools, never model-written SQL | One injection away from a cross-tenant leak otherwise | Medium |
| 18 | No number without a defensible source; a component with no source is absent, not zero | The credibility of every other number depends on it | None |
| 19 | Deterministic values are reproducible; model output records accountability instead | An LLM is not a function, and a schema claiming otherwise sends someone hunting a bug that does not exist | Low |
| 20 | **`crawl_allowed` is derived, never user-controlled, and enforced immediately before crawl dispatch** (M1) | See below | Low |

### On decision 20

```
crawl_allowed = ownership_verified
                AND ownership coverage matches the crawl target
                AND website status is active
```

**No database trigger.** A trigger cannot see facts that live outside Postgres
— plan state, robots.txt, an operator suspension — so it would be either
incomplete or would reach further than a trigger should.

Enforced at the service boundary instead:

```
request crawl → load website → evaluate crawl_allowed()
              → false: reject, no job created
              → true:  enqueue
```

**And the crawler independently re-checks before fetching.** Defence in depth:
the gate that admits the job and the gate that starts the work are separate, so
a queue entry cannot crawl a website whose verification was revoked after it
was enqueued.

The column is kept for observability, and clients cannot write it — not by
convention but by grant (migration `0012`), which matters because under
Supabase a client role reaches Postgres directly. Worth recording the mechanism:
a column-level `REVOKE` does **not** subtract from a table-level `GRANT UPDATE`,
so the protection is a table-level revoke followed by a column-by-column
re-grant of everything else. The first implementation used the obvious spelling
and the test caught it.

### On decision 13, concretely

```python
class BacklinkProvider(Protocol):
    def get_domain_backlinks(self, domain: str, *, since: date | None) -> Iterable[Backlink]: ...
    def get_referring_domains(self, domain: str) -> Iterable[ReferringDomain]: ...
    def get_anchor_text(self, domain: str) -> Iterable[AnchorDistribution]: ...
    def get_new_lost_links(self, domain: str, window: DateRange) -> Iterable[BacklinkChange]: ...
    def get_competitor_backlinks(self, domain: str, competitor: str) -> Iterable[Backlink]: ...
```

`Backlink`, `ReferringDomain` and `BacklinkChange` are the platform's types,
defined in `0007` and populated from whichever vendor is contracted. Vendor
choice is a **commercial decision about COGS and coverage**, made when the
quotes are in — it is not on the critical path for any architectural work, and
nothing in `api/domain/` will change when it is made.

The same shape applies to `KeywordDataProvider`, `SerpProvider` and
`TrafficEstimateProvider`. `data_providers.unit_cost_usd` carries the
commercial answer back into plan admission once it exists.

## Open — commercial and product, not architectural

1. **Backlink vendor** (DataForSEO / Ahrefs / Majestic / Moz). Sets Authority's
   COGS and the Professional tier's floor. Evaluate during M3–M5; it blocks no
   code.
2. **Business Profile in phase 3?** The API needs separate approval with its own
   eligibility bar and is the only write scope in the plan. For a local business
   the Maps pack *is* their visibility, so it is a strong differentiator — but
   apply early, because approval is slow.
3. **SERP rank tracking — ever?** GSC covers the user's own website honestly and
   free. Live SERP tracking adds competitor positions at real cost and real
   terms-of-service exposure. The biggest "are we that kind of company" question
   in the plan.
4. **Region and data residency.** Choosing now is free; splitting later is not.
   `organizations.region` exists for when it matters.
5. **Free-tier page cap.** 100 keeps costs near zero but truncates most real
   websites, and a truncated audit undersells the product. Measure rather than
   guess.
6. **Ownership of generated content**, in the terms, before phase 3 ships it.

## Things deliberately not done

- No auto-publishing path for generated content, at any tier. Publication
  requires an approved `actions` row; there is no status that bypasses it.
- No write access to customer websites before phase 3, and then only capabilities
  marked `reversible` are ever eligible for standing approval.
- No `website:` queries or SERP scraping for competitor page counts — a
  competitor's own sitemap is legitimate and more accurate.
- No modelled traffic figures presented as fact. `external_metrics.is_modelled`
  makes the distinction structural rather than editorial.
- No robots.txt override flag in the crawler, because an override flag
  eventually gets used.
