# 05 — Analysis and scoring

## Detection is code

Every finding comes from a deterministic rule that emits a stable fingerprint.
The model is not in this path. It writes the explanation afterwards, from the
evidence the rule produced.

A rule is:

```python
@rule(key="ctr_below_position_baseline", scope="page", category="seo")
def check(ctx: SiteContext) -> Iterable[Finding]:
    for page in ctx.gsc_pages(window=days(28), min_impressions=200):
        expected = ctx.expected_ctr(page.position)
        if page.ctr < expected * 0.6:
            yield Finding(
                scope_ref=page.url_hash,
                severity="high" if page.impressions > 2000 else "medium",
                evidence={
                    "impressions": page.impressions,
                    "clicks": page.clicks,
                    "ctr": page.ctr,
                    "position": page.position,
                    "expected_ctr": expected,
                    "clicks_at_expected": round(page.impressions * expected),
                },
                impact=(expected - page.ctr) * page.impressions,
            )
```

The `impact` value is the ranking currency across the whole system: **estimated
additional monthly clicks**. Comparable across rule types, defensible to a
user, and arithmetic rather than opinion.

## The CTR baseline, because this one rule can ruin the product

"4,821 impressions, 37 clicks" is 0.77%. At position 3 that is dreadful. At
position 18 it is completely normal. A rule that flags low CTR without a
position baseline generates hundreds of non-issues, and a user who learns to
ignore the recommendation list has left the loop for good.

`expected_ctr(position)` is derived per website from that website's own
`gsc_query_daily`, bucketed by integer position over the trailing 90 days,
falling back to a global curve when a bucket has fewer than 50 impressions:

```
position:  1     2     3     4     5     6     7     8     9    10   11-20
ctr:      .27   .15   .10   .07   .05   .04   .03   .025  .02  .018  .01
```

Per-website derivation matters because branded and local niches have wildly
different curves from the global average. A church's own name is a 70% CTR at
position 1; a comparison query is 12%.

## Issue catalogue (MVP)

Seeded into `issue_types`. `w` is the weight inside its scorecard component.

### Technical

| key | severity | w | Trigger |
| --- | --- | --- | --- |
| `page_5xx` | critical | 5 | Status ≥ 500 |
| `page_404_internal_link` | critical | 4 | Internally linked URL returns 404 |
| `robots_blocks_crawl` | critical | 5 | robots.txt disallows the whole website |
| `noindex_on_valuable_page` | critical | 5 | `noindex` on a page with GSC impressions |
| `redirect_chain` | medium | 2 | Chain length ≥ 3 |
| `canonical_to_noncanonical` | high | 3 | Canonical target is itself canonicalised elsewhere |
| `missing_viewport` | high | 3 | No viewport meta |
| `no_sitemap` | medium | 2 | No sitemap in robots.txt or at the conventional paths |
| `sitemap_contains_errors` | medium | 2 | Sitemap lists non-200 or non-canonical URLs |
| `slow_lcp` | high | 3 | CrUX field LCP p75 > 2.5s |
| `oversized_image` | medium | 2 | Any image > 500 KB |
| `mixed_content` | high | 3 | HTTP subresource on an HTTPS page |

### Content

| key | severity | w | Trigger |
| --- | --- | --- | --- |
| `missing_title` | critical | 5 | Empty or absent `<title>` |
| `duplicate_title` | high | 3 | Same title on ≥ 2 indexable pages |
| `title_length` | low | 1 | < 30 or > 60 characters |
| `missing_meta_description` | high | 2 | Absent on an indexable page |
| `duplicate_meta_description` | medium | 2 | Same description on ≥ 2 pages |
| `missing_h1` / `multiple_h1` | medium | 2 | Zero or ≥ 2 `<h1>` |
| `thin_content` | medium | 2 | < 200 words on an indexable page |
| `duplicate_content` | high | 3 | Equal `text_hash` across distinct canonicals |
| `images_missing_alt` | medium | 2 | > 20% of images lack `alt` |
| `orphan_page` | medium | 2 | Zero internal inlinks, present in sitemap |

### SEO / opportunity — these need GSC, which is why it is connected first

| key | severity | w | Trigger |
| --- | --- | --- | --- |
| `ctr_below_position_baseline` | high | 4 | Measured CTR < 60% of expected at that position |
| `striking_distance_keyword` | high | 4 | Position 11–20, ≥ 100 impressions/month |
| `declining_page` | high | 3 | Clicks down ≥ 30% over 28 days vs the prior 28, seasonality-adjusted |
| `rising_keyword` | info | 1 | Impressions up ≥ 50%, for the "gaining visibility" panel |
| `cannibalisation` | medium | 2 | ≥ 2 URLs alternate as the ranked page for one query |
| `no_internal_links_to_strong_page` | medium | 2 | High-impression page with < 3 internal inlinks |

### AI visibility (proxies — computable every crawl)

| key | severity | w | Trigger |
| --- | --- | --- | --- |
| `ai_crawler_blocked` | high | 4 | robots.txt disallows GPTBot / ClaudeBot / PerplexityBot / Google-Extended |
| `no_structured_data` | high | 3 | No JSON-LD on key templates |
| `missing_organization_schema` | medium | 2 | No `Organization` / `LocalBusiness` on the homepage |
| `content_requires_js` | high | 3 | Main content absent from the raw HTML |
| `no_author_or_entity_clarity` | low | 1 | No author, publisher or `sameAs` links |

These are honest proxies for machine readability. They are labelled as
*modelled*, not measured. Live measurement arrives in v3.

## Scoring

```
total = round(
    0.30 * technical_seo
  + 0.30 * google_visibility
  + 0.25 * content
  + 0.15 * ai_visibility
)
```

Authority is excluded until backlink data is licensed, and the UI shows it as
"not available on your plan" rather than as a number.

Each component is `100 - penalty`, where penalty sums the weights of open
issues in that category, normalised by the number of pages evaluated so a
1,000-page website is not punished for having more of everything:

```
penalty = min(100, 100 * Σ(w_i * severity_multiplier_i * affected_i / evaluated_i) / Σ(w_i))
severity_multiplier = {critical: 1.0, high: 0.7, medium: 0.4, low: 0.15, info: 0}
```

`google_visibility` is not penalty-based — it is measured, blending 28-day
click trend, impression trend, average position change and the share of
tracked keywords in the top 10, each normalised against the website's own 90-day
history rather than a cross-customer benchmark.

### Rules for changing the scoring model

1. Bump `scoring_version`.
2. Recompute **every** historical `score_snapshot` under the new version.
3. Render the chart from a single version throughout.
4. Tell the user in the weekly report that the model changed.

A score that moves because weights changed silently is a lie told in a chart,
and it is the single fastest way to lose the users who care most.

## AI visibility measurement (v3)

The measured half, when it ships, follows one rule above all:

**Never report a boolean.** LLM answers are non-deterministic; the same query
returns different sources run to run. Each query runs N times (default 8) per
provider, and the stored metric is a **mention rate** — "mentioned in 6 of 8
runs" — with citation URLs and competitor mentions captured per run. That is
stable enough to trend honestly; a binary flag flickers and the user catches
it.

Measurement goes through providers' APIs with web search enabled, and every
number is labelled with the surface and date it came from. Scraping consumer
chat interfaces is out: it violates terms, breaks constantly, and is not a
foundation for a paid metric.

Cost is the design constraint: `queries × runs × providers × cadence`. Weekly,
on a tight query set the user chooses, with per-plan caps.
