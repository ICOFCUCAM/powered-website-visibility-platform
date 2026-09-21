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

```
You explain website SEO problems to a non-technical business owner.

You will receive one detected issue as JSON, including the evidence that
produced it. Write:
  - "what": one sentence naming the problem in plain language
  - "why": one or two sentences on why it costs them visibility
  - "how": concrete numbered steps to fix it on their website
  - "effort": one of low | medium | high

Rules:
  - Use ONLY numbers present in the evidence. Never estimate, extrapolate or
    invent a figure. If you want a number you were not given, omit the claim.
  - Never promise a ranking improvement. Describe the change, not the outcome.
  - No jargon without a plain-language gloss on first use.
  - British English. Second person. No preamble, no sign-off.

Return JSON: {"what": str, "why": str, "how": [str], "effort": str}
```

Cache key: `sha256(prompt_version + type_key + canonicalised_evidence)`. Two
websites with the same missing-title problem share one generation; a website whose
evidence is unchanged since last week regenerates nothing. On a 500-page website
this is the difference between ~200 calls and ~5 per crawl.

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

| Purpose | Tier | Why |
| --- | --- | --- |
| Issue explanations (bulk) | cheap | High volume, low reasoning, heavily cached |
| Keyword expansion | cheap | Generative breadth, no reasoning |
| Weekly plan | frontier | Synthesis across ranked findings and history |
| Strategist | frontier | Multi-step tool use |

Controls, all enforced before the call, not after the bill:

- `organizations.monthly_ai_budget_usd`, checked at admission. Over budget, bulk
  explanations fall back to templates and the user is told their plan's AI
  allowance is used up; the weekly plan still runs, because it is the product.
- Prompt caching on the long, stable system prompts.
- Explanation cache keyed on evidence, as above.
- Per-message tool-call cap for the Strategist.
- `llm_calls` gives cost per feature per org, so a feature whose unit economics
  do not work is visible in week one rather than at renewal.
