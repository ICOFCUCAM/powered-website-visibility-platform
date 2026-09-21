# 06 — AI layer

Four jobs, each with a different cost profile and a different failure mode:

| Job | Model tier | Cadence | Grounding |
| --- | --- | --- | --- |
| Issue explanation | cheap/fast | per new issue, cached | rule evidence only |
| Weekly action plan | frontier | weekly per website | ranked findings + GSC deltas |
| Keyword expansion | cheap/fast | on demand | business description + GSC queries |
| AI Strategist (chat) | frontier | interactive | tool calls over the user's own data |

## Three rules

**1. The model never invents a number.** Every figure in generated prose comes
from a value passed into the prompt. Search volumes it has not been given, rank
predictions, traffic estimates — all forbidden, and all checked.

**2. The model never decides priority.** Ranking is `impact_score`, computed by
code. The model orders the prose to match the ranking it is handed. If the
model picked, the same website would produce a different top four on Tuesday than
on Monday, and neither could be explained.

**3. Output is schema-validated.** Every call returns structured JSON against a
schema. A validation failure retries once, then falls back to a template
rendering of the raw evidence. A user seeing a plain templated finding is fine;
a user seeing a hallucinated one is not.

## Post-generation validation

Before any generated text is stored, an automatic check extracts every number
from the output and asserts it appears in the input evidence (within rounding).
Failures are logged with `status='refused'` on `llm_calls` and the templated
fallback is used. This is cheap, catches the failure mode that matters most,
and gives a measurable hallucination rate per prompt version.

## Prompts

All prompts are versioned files under `api/ai/prompts/`, and the version is
written to `plans.prompt_version` / `llm_calls.prompt_version`. Changing a
prompt without bumping the version makes past output unreproducible.

### `issue_explanation.v1`

**The cache unit is the PROBLEM, not the page.** This changed during
implementation and it is the difference between the cost claim below being
true and being wishful. Evidence for a page-scoped finding contains that
page's URL, so keying on raw evidence would give a 500-page site with 200
missing titles two hundred distinct keys and two hundred calls — exactly the
bill the cache exists to avoid.

So the prompt receives, per issue type: the catalogue entry, the thresholds
the rule actually applied (imported from the rule modules, never retyped), and
an allowlisted set of facets — a position *band* rather than a position, "too
short" rather than a character count. A field not named in that allowlist
cannot reach the prompt. Everything page-specific is rendered beside the
explanation by code that cannot get it wrong.

Two things fall out of that, and both matter more than the saving:

- **The model is never shown a customer's URL, page title or search query**,
  so it cannot repeat one, mis-transcribe one or invent one.
- **The thresholds make the advice concrete without breaking rule 1.** "Aim
  for 30 to 60 characters" is grounded, because 30 and 60 came from the rule
  that raised the finding. Without them the validator correctly refuses every
  useful number, and the advice degrades to "an appropriate length".

```
You explain website SEO problems to a non-technical business owner.

You will receive one kind of detected problem as JSON, with the general facts
that produced it. Write:
  - "what": one sentence naming the problem in plain language
  - "why": one or two sentences on why it costs them visibility
  - "how": concrete numbered steps to fix it on their website
  - "effort": one of low | medium | high

Rules:
  - Use ONLY numbers present in the input. Never estimate, extrapolate or
    invent a figure. If you want a number you were not given, omit the claim.
  - You have not been shown their pages, titles or search terms. Do not refer
    to a specific page, URL or phrase as though you had.
  - Never promise a ranking improvement. Describe the change, not the outcome.
  - No jargon without a plain-language gloss on first use.
  - British English. Second person. No preamble, no sign-off.

Return JSON: {"what": str, "why": str, "how": [str], "effort": str}
```

Cache key: `sha256(prompt_version + canonicalised_payload)`, where the payload
*is* the prompt — the prompt text is the canonical JSON. That makes cache
correctness structural rather than remembered: a field cannot enter the prompt
without entering the key.

`issue_explanations` is therefore global and carries no `organization_id`, the
only tenant-bearing-looking table in the schema that does not. Two
organisations collide only when the text each would have been shown is
byte-identical, so a shared row cannot carry one tenant's data to another.
Adding a tenant column would not make it safer, only more expensive: every
site would pay for its own copy of the same sentence. The decision is argued
in migration 0017 and asserted in `test_rls_backstop.py`.

On a 500-page website this is the difference between ~200 calls and ~5 per
crawl. A site whose evidence is unchanged since last week regenerates nothing;
so does a site whose problem somebody else already had.

### `weekly_plan.v1`

```
You are an SEO consultant writing this week's action plan for one website.

You receive:
  - website: domain, business description
  - period: this week's dates
  - findings: ALREADY RANKED by measured impact, each with evidence
  - performance: 28-day clicks, impressions, CTR, position, with deltas
  - last_week: what you recommended last week and what happened to each item

Produce at most FOUR priorities, in the order given. Do not reorder. Do not
introduce a priority that is not in `findings`.

For each: a title an owner could act on today, why it matters (citing only the
supplied numbers), the specific pages or keywords, and the expected effect in
clicks where `estimated_clicks_delta` is present.

Open with two sentences on what changed since last week, naming anything from
last week's plan that was completed or that regressed.

Rules:
  - Only supplied numbers. Never invent search volume or predict a position.
  - If a previous recommendation was done and the metric did not move, say so
    plainly. Do not claim credit for changes the data does not show.
  - British English, direct, no filler.

Return JSON matching the WeeklyPlan schema.
```

The `last_week` block is what makes the consultant a consultant rather than a
report generator: it is a join over the prior `plans` row, `recommendations`
statuses and `issue_observations` since that date.

**Refs, not trust.** Each finding is handed to the model with a `ref`, and the
generation is accepted only if its `priorities` carry the same refs in the same
order. Rule 2 is enforced by comparing two lists, not by the prompt asking
nicely. A reordered, merged, dropped or invented priority is rejected whole and
the plan falls back to its templated text with `fallback_reason =
'reordered_priorities'`.

### `keyword_expansion.v1`

```
Given a business description and the queries this website already receives
impressions for, propose up to 40 additional search phrases real people would
type.

Rules:
  - Output phrases ONLY. Never output search volume, difficulty or traffic
    estimates: you do not have that data and a fabricated figure is worse than
    no figure.
  - Group by intent: informational | commercial | navigational | local.
  - Prefer phrases consistent with the website's existing query profile.
  - Include local variants where the business description names a place.

Return JSON: {"groups": [{"intent": str, "phrases": [str]}]}
```

Candidates are then ranked by *the website's own* GSC impressions where the
phrase already appears — a free, honest signal that requires no third-party
data. Unranked candidates are shown as unvalidated suggestions, never with a
made-up number beside them.

## AI Strategist

Conversational, over the user's own data. The user asks "why did my traffic
fall?" and gets an answer from their numbers, not generic advice.

**It does not write SQL.** The model calls a fixed set of typed tools, each a
parameterised query with the org scope bound server-side from the session — not
from a model argument. A model that emits SQL against a multi-tenant database
is one prompt injection away from a cross-tenant leak.

| Tool | Returns |
| --- | --- |
| `get_performance_summary(period, compare_to)` | clicks, impressions, CTR, position, deltas |
| `get_top_queries(period, limit, order_by)` | query rollups |
| `get_top_pages(period, limit, order_by)` | page rollups |
| `get_movers(period, direction, dimension)` | biggest gains/losses by query or page |
| `get_open_issues(category, limit)` | ranked findings with evidence |
| `get_page_detail(url)` | latest snapshot, issues, GSC history |
| `get_keyword_history(phrase, period)` | daily series |
| `compare_periods(metric, a, b)` | aligned series for two windows |
| `get_site_changes(period)` | crawl diffs, applied fixes, verification results |

Guardrails: tools are read-only; row caps on every return; the org scope comes
from the authenticated session; the system prompt states that tool results are
data and never instructions; a turn budget caps tool calls per message; and
every call is metered to `llm_calls` against the org's monthly budget.

Answer style: lead with the finding, cite the numbers used, name the pages or
queries, end with one concrete next step. When the data does not support a
conclusion, say that — "your traffic fell but the drop is entirely in one query
that is seasonal; there is no website problem in this data" is a better answer
than a confident wrong one.

## Model routing and cost control

| Purpose | Tier | Model | Why |
| --- | --- | --- | --- |
| Issue explanations (bulk) | cheap | `claude-haiku-4-5-20251001` | High volume, low reasoning, heavily cached |
| Keyword expansion | cheap | `claude-haiku-4-5-20251001` | Generative breadth, no reasoning |
| Weekly plan | frontier | `claude-opus-5`, adaptive thinking | Synthesis across ranked findings and history |
| Strategist | frontier | `claude-opus-5`, adaptive thinking | Multi-step tool use |

The cheap tier sends no `thinking` block: bulk explanation is formatting over
evidence the rules engine has already reasoned about, and thinking tokens on it
are spend without a product. Both tiers use structured outputs
(`output_config.format`) rather than an instruction to reply with JSON; a model
that rejects the parameter is retried once without it, with the schema appended
to the system prompt, and our own validation runs on either path.

Controls, all enforced before the call, not after the bill:

- `organizations.monthly_ai_budget_usd`, checked at admission. Over budget, bulk
  explanations fall back to templates and the user is told their plan's AI
  allowance is used up; the weekly plan still runs, because it is the product.
- Prompt caching on the long, stable system prompts.
- Explanation cache keyed on evidence, as above.
- Per-message tool-call cap for the Strategist.
- `llm_calls` gives cost per feature per org, so a feature whose unit economics
  do not work is visible in week one rather than at renewal.

## Degrading honestly

There may be no provider at all — no API key, or an installation that has not
configured one — and that is a first-class state rather than a
misconfiguration. Every caller in the layer has a template path drawn from the
issue catalogue and the rule thresholds, so the product produces plain, correct
advice and a plan whose figures are entirely real.

The order of preference is `cache → model → template`, and each step is a
normal outcome:

| `fallback_reason` | What happened |
| --- | --- |
| `no_provider` | No model is configured |
| `budget_exhausted` | The org's monthly allowance is spent (bulk only) |
| `provider_error` | The call failed; recorded as `status='error'` |
| `validation_failed` | The output did not match the schema |
| `unsupported_numbers` | A figure in the output is not in the evidence |
| `reordered_priorities` | The generation changed the ranking |

Two rules the fallback has to obey as strictly as the model does:

1. **A refusal is never cached.** The generation that invented a figure does
   not reach the customer and does not reach anybody else's cache either.
2. **The templates are held to rule 1.** A hand-written fallback quoting
   "roughly 150 characters" for a length we never measure is the same
   fabrication, just ours. `test_ai_explanations.py` runs every catalogue
   entry's template through the numbers validator.

`plans.model` and `recommendations.prose_source` record which path produced the
words, so a customer reading templated advice while believing a model wrote it
— or the reverse — is a difference the row can answer.
