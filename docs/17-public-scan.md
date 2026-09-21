# The public scan

The hero used to show an invented dashboard with a badge saying so, because a
product whose first rule is *never fabricate a number* cannot put unlabelled
ones on its own front page. This is the better answer: put the visitor's own
site on it. Type a domain, get one page checked live, and the disclaimer comes
off because there is nothing left to disclaim.

`POST /api/v1/peek` — the one route in the product that serves somebody with
no account.

## What makes it different

Everything below follows from there being no account behind the request.

**It fetches one page.** No frontier, no sitemap, no links followed. A
stranger cannot spend our bandwidth on a thousand pages of somebody else's
site.

**It writes nothing.** No organisation, no website row, no pages, no
findings, no crawl. The result exists for the length of the response.
`test_peek.py::test_nothing_is_written_anywhere` counts rows either side of a
scan and fails if any moved.

**It does not re-implement a single rule.** The findings come from
`api.analysis.rules` — the same code the full audit runs — over an
`AnalysisContext` holding exactly one page. A peek can therefore never
disagree with what the real audit would say about the same page.

## The guard

The crawler has always been gated on `ownership_verified_at`: it visits a
site only after the customer proved they own it. **That gate is the reason no
URL guard existed.** A public endpoint has no such gate, and a server that
fetches any URL on request is a server that will read its own cloud metadata
endpoint and hand the credentials to whoever asked.

`api/crawler/safety.py` is that guard. A URL is acceptable only if it is
http(s), on port 80 or 443, carries no `user:password@`, names a host that is
not an internal name, and resolves **entirely** to globally routable
addresses. `is_global` is the single rule that carries the weight — it is
false for private ranges, loopback, link-local (where `169.254.169.254`
lives), carrier-grade NAT, benchmark and reserved space, and multicast.

DNS can lie twice, so there are two checks:

- **`check_url`** runs before the request, and again on every redirect hop —
  redirects are followed by hand precisely because "302 to 169.254.169.254"
  is the reason a guard that only inspects the first URL is not a guard.
- **`check_peer`** runs once the socket is open and the headers are in, but
  *before any body is read*, and looks at the address actually connected to.
  This closes the rebinding window that every allowlist built on DNS alone
  leaves open. It **fails closed**: no peer information is a refusal.

`api/tests/test_url_safety.py` is a list of attacks rather than a list of
functions — the metadata endpoint, loopback in seven spellings, `file://`,
`gopher://`, credential smuggling, odd ports, internal suffixes, bare
hostnames, a private DNS answer, and one private answer hiding among public
ones.

**Errors do not say why.** "That is a private address" and "that does not
resolve" are different answers, and telling them apart turns this into a port
scanner with a nice interface. Every refusal is the same sentence.

## What it costs us

`api/peek/limits.py`, Redis-backed, two limits because they stop different
things:

| | Limit | Window | Stops |
| --- | --- | --- | --- |
| Per visitor (IP) | 5 | 5 min | one person sitting on the button |
| Global | 60 | 1 min | a thousand IPs doing it at once |

The global one is the real ceiling: whatever it is set to is the most
outbound fetching this endpoint can ever do in a minute, regardless of who is
asking. Both **fail closed** — if Redis is unreachable we cannot count, and
an endpoint that fetches arbitrary URLs is not something to leave uncounted.
Body is capped at 2 MB, timeout at 8 seconds, redirects at 3.

## The rules it runs

Fourteen of the twenty-seven — the ones that can say something true about a
single anonymous page. Rules needing Google data, history, or a second page
are **absent rather than silent**: a rule with nothing to look at is a rule
invited to guess.

```
missing_title · title_length · missing_meta_description · missing_h1
multiple_h1 · thin_content · images_missing_alt · missing_viewport
no_structured_data · missing_organization_schema · content_requires_js
page_5xx · redirect_chain · canonical_to_other_page
```

That list was once written from memory and named fourteen rules that **did
not exist**, so every scan found nothing and looked like it worked.
`test_every_named_rule_is_a_real_rule` exists because of that afternoon: a
rule key that does not resolve is a silent no-op, and silence is
indistinguishable from a clean page.

No score is shown for a scan. One page cannot produce a visibility score, and
inventing one for the sake of a filled-in dashboard is the thing this product
exists not to do.

## The contract

> **The public scan cannot touch the database.**
> `api.peek` may not import `api.adapters.db`, `api.repositories`, `api.deps`,
> `api.hub`, `api.workers`, `api.alerting` or `api.ai`.

Direct imports only, for the same reason as the model contract: running the
real rules is the design, and `api.analysis.rules.base` reaches the database
through `ctr_baseline` for a curve this never builds. What is forbidden is
`api.peek` itself opening a connection or resolving a tenant.

## Not yet

**Streaming.** The scan is one request and one response. A progress stream
("checking the address… fetching… reading") would make a two-second wait feel
like work being done rather than a spinner.

**robots.txt.** We do not check it before the fetch. For one page the visitor
themselves asked about this is defensible, but it is inconsistent with the
crawler, which obeys it — and `ai_crawler_blocked` is one of the better
findings this product has, so fetching robots.txt would earn its second
request.

**Caching.** The same domain scanned twice in a minute is fetched twice.
