# 03 — Crawler

The crawler is the only component that touches the open internet under the
platform's name. Its reputation is the platform's reputation, so politeness is
a correctness requirement, not a courtesy.

## Shape

```
schedule/onboarding
        │
        ▼
   crawls row ──► seed job ──► crawl_frontier (pending)
                                   │
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
        fetch worker         fetch worker        render worker pool
        (httpx, n=8)         (httpx, n=8)        (Playwright, n=2)
              │                    │                    │
              └────────────────────┼────────────────────┘
                                   ▼
                          extract → page_snapshots
                                   │           └──► raw HTML to object storage
                                   ▼
                          link rows → page_links
                                   │
                                   ▼
                        new URLs → crawl_frontier
                                   │
                          (frontier empty)
                                   ▼
                        analyse → issues → score → plan
```

## Seeding

1. `GET /robots.txt` — parse rules and `Sitemap:` directives; store the raw file.
2. Sitemaps, recursively through index files, capped at 50 files / 50k URLs.
3. The site origin, plus any origin from a homepage redirect chain.

If robots.txt is unreachable, treat it as "allow all" but record the fact. If
it disallows everything, stop and raise a `robots_blocks_crawl` issue — that is
a finding for the user, not a crawler failure.

## Politeness

Non-negotiable defaults:

- **One concurrent request per host.** Not per crawl — per host. Two crawls of
  the same host share the limit via a host token bucket.
- **Default delay 1s**, and `Crawl-delay` is honoured when present, up to 30s.
  Beyond 30s, cap and tell the user the crawl will be slow.
- **Identifiable user agent** with a contact URL:
  `VisibilityBot/1.0 (+https://<domain>/bot)`. That page must exist and must
  explain how to block the crawler.
- **`robots.txt` respected for fetching**, always, including for competitor
  crawls in v2. There is no override flag, because an override flag eventually
  gets used.
- **Back off on 429/503** with exponential delay and abandon the host for the
  run after repeated refusals.
- **Verified ownership required** before crawling beyond the plan's free page
  cap, via the GSC link or a DNS/file token. This stops the platform being used
  as a stress-testing service against sites the user does not own.

## Render escalation

Default is a plain `httpx` fetch. Headless Chromium costs roughly 50× the
memory and time, so it is a per-URL decision, not a per-project one:

Escalate when any of these hold:
- rendered-vs-raw heuristic trips: `<body>` text under ~200 chars while script
  bytes exceed ~50 KB;
- the HTML has an SPA root (`#root`, `#app`, `<app-root>`) and no `<h1>`;
- the URL matches a site-level "always render" override;
- a sample of 5 URLs per crawl is always rendered to detect a JS-dependent
  template, and if ≥3 differ materially, the whole crawl escalates.

Budget: 50 rendered pages per crawl by default, plan-configurable. Render
workers run in their own pool with hard memory limits — Playwright OOMs are
the classic way a shared worker pool takes the analysis pipeline down with it.

## Extraction

Per page, into `page_snapshots`: status and redirect chain, title and meta
description with lengths, headings, word count, canonical, `robots` meta and
`X-Robots-Tag`, `hreflang`, viewport, JSON-LD/microdata types with validation
errors, Open Graph, image counts including missing `alt` and oversized bytes,
and internal/external link counts.

Two hashes are stored: `content_hash` over the normalised HTML and `text_hash`
over extracted text. Equal hashes between crawls mean nothing changed — which
skips re-analysis, allows cached LLM explanations, and powers duplicate-content
detection within the site.

Raw HTML is gzipped to object storage at
`{site_id}/{crawl_id}/{url_hash}.html.gz`. Postgres stores the key.

## Link checking

Internal links come from `page_links`. External links are HEAD-checked with a
separate, slower, heavily rate-limited pass, deduplicated by target host, and
only broken results are retained. Checking every external link on every crawl
is both wasteful and rude.

## Failure and resumption

Every fetch failure is classified — `dns`, `tls`, `timeout`, `connreset`,
`http_4xx`, `http_5xx`, `robots_denied`, `too_large`, `non_html` — and counted
on the crawl. Three attempts with backoff, then `failed` in the frontier.

The crawl's own liveness is a `heartbeat_at` column. A supervisor re-leases
expired frontier rows from crawls whose heartbeat has gone stale, so an
evicted worker costs minutes, not a whole crawl.

A crawl ends when the frontier drains or the page cap is hit, whichever comes
first. Hitting the cap is recorded and shown to the user — a truncated crawl
that looks complete produces analysis that is quietly wrong.

## Scheduling

Weekly by default, staggered by a hash of `site_id` across the week so Monday
09:00 is not a thundering herd. Daily on higher plans. Verification crawls are
single-URL, run within minutes of a fix being marked applied, and write to
`crawls.verifies_issue_id`.
