# Alerting

A failed run has been recorded and visible since the scheduler shipped.
Nothing told anyone. This is what tells someone.

The design is one sentence: **the failure mode of alerting is noise, and the
second failure mode is silence.** Everything below is the line between them.
An operator who has learned to ignore the channel has the same monitoring as
one with no channel; an alert that fires once and then goes quiet while the
problem continues is worse than one that repeats, because silence reads as
recovery.

## Two audiences, deliberately separated

The distinction is the whole design.

| | Operator | Customer |
|---|---|---|
| Says | the machine is broken | something only you can fix |
| Example | `sync_search_console` failed for 400 of 412 websites | Google has stopped accepting our access to Search Console for example.com |
| Lives in | `operator_alerts` | `alert_events` |
| Goes to | a webhook and/or an address we own | the organisation's members, by email |
| Scoped by | nothing — it is fleet-wide | `organization_id`, like all tenant data |
| Repeats | every 6 hours while unresolved | once per 14 days per (website, kind) |
| Resolves | yes, with a recovery notice | no — the next one is a new event |

**A customer is never told about our problems.** A sync that failed because
Google returned 503, a worker that died, a crawl that hit our own timeout —
those are ours. A customer who receives an alert they cannot act on learns to
ignore the ones they can. Every customer notice therefore carries an
`action`, structurally: a notice without one is just bad news.

The converse holds too. One customer revoking access is normal and is their
business; it produces a customer notice and no operator alert. A *quarter of
the fleet* needing re-auth at the same time is an OAuth client that has been
suspended, a rotated secret, or a scope change — and no number of customer
emails fixes it, so that one is ours.

## What is worth an alert

`api/alerting/operator.py::scan()` is a pure read. It takes a connection and a
clock and returns a list of `Condition`; it sends nothing, so the whole
question of *what is worth saying* is testable without a network.

- **`scheduler_quiet`** — critical. Nothing has been claimed in 8 hours and
  there are active websites. **This is the most important check in the file**,
  because a scheduler that has stopped makes every other alert silent too: no
  runs, no failures, nothing to report. It is the one condition that catches
  its own absence. Guarded on there being websites at all, so a fresh install
  with nothing in it does not page anybody on day one.
- **`jobs_unclaimed`** — a job whose failures are *all* the reaper's
  "no worker picked this up". Separated from the one below on purpose:
  "no worker picked this up" and "the job threw" send an operator to
  completely different places — a pool that is down, versus a bug.
- **`job_failing`** — a job that is throwing. One condition per job, never one
  per website.
- **`crawls_failing`** — the crawler, fleet-wide.
- **`connections_need_reauth`** — three or more, and at least a quarter of all
  live connections.

Severity is a ratio, not a count: at or above half the runs (and at least
three) it is critical, below that it is a warning. Four hundred failures at
01:07 produce **one** message, aggregated in `scan` before anything is sent.
The alternative is four hundred messages, which is the same as none.

Customer notices, in `api/alerting/customer.py::find()`:

- **`connection_needs_reauth:<service>`** — the one that matters most. Their
  dashboard keeps showing yesterday's numbers, which looks like nothing is
  wrong; without a message they find out in a month when somebody notices the
  chart is flat. Raised per *website*, not per connection, because a customer
  thinks in websites.
- **`crawl_failing`** — the last three crawls of one website all failed. Only
  fires when the failures are concentrated on one site; a crawler broken for
  everybody is an operator problem and is reported as one.

## An alert is an incident

`operator_alerts` is keyed by a **fingerprint** — `sha256(kind:subject)` — and
never by a count or a timestamp, or every scan would open a new incident. A
partial unique index makes the open incident unique:

```sql
create unique index operator_alerts_open on operator_alerts (fingerprint)
    where resolved_at is null;
```

which lets `reconcile` upsert: `on conflict (fingerprint) where resolved_at is
null do update set occurrences = occurrences + 1, …`. Because the index covers
only *open* rows, a problem that recovers and returns opens a new incident
rather than resurrecting the old one, and the history is kept.

The lifecycle, then:

1. **Opened** on the first scan that sees it, and announced.
2. **Counted** on every scan after that, and *not* announced. Fifteen minutes
   apart for a week is how a channel gets muted.
3. **Mentioned again** after 6 hours, with how long it has been going on —
   "it happened once at 3am" and "it has been happening all week" are
   different problems.
4. **Resolved** when a scan stops seeing it, with a recovery notice. Only
   where the problem was announced: resolving something nobody was told about
   is not news.

**Deliver, then record — never the other way round.** A delivery that fails
leaves `notified_at` null, so the incident is announced again on the next scan
rather than going quiet having told nobody. The same applies to
`NullNotifier`: no destination configured means nothing was delivered, so
nothing is marked as sent, and the day somebody configures a destination every
open incident is announced.

**One clock.** `first_seen_at`, `last_seen_at` and `notified_at` all come from
the scan's clock, not the database's. They are compared against each other and
against the re-notify interval, so mixing two clocks makes a fresh incident
look hours old — and makes the re-notify interval measure the difference
between the two rather than elapsed time.

## Where it goes

`ALERT_WEBHOOK_URL` for a chat channel, `ALERT_EMAIL` (comma separated) for
email, both for both, neither for an honest nothing. The webhook payload is
Slack-*compatible* rather than Slack-specific — `{"text": …}` is what Slack,
Mattermost, Discord's Slack endpoint and most generic receivers accept — so
the destination is a configuration change, not a code change.

**Nothing tenant-identifying goes to a webhook.** An operator alert says
"12 of 40 runs failed", never which twelve. A chat channel is not a place for
customer domains, and an operator who needs the list queries `scheduled_runs`,
where it is already scoped and audited.

A notifier is deliberately dumb: it renders and posts. It does not decide
whether to alert, does not deduplicate and does not rate limit. All of that is
in `operator.py`, where it is tested without a network.

## Tenancy

`operator_alerts` is the second table in the schema with no
`organization_id` — `deletion_receipts` is the other — and for a related
reason: an incident like "sync failed for 400 of 412 websites" belongs to
nobody's organisation. Scoping it by one would either split the aggregate back
into four hundred alerts or attribute the whole fleet's failure to whichever
tenant happened to be first. Its select policy is `using (false)`, so the
request-path role reads none of it; only the service role, which never serves
a browser, sees it at all. `api/tests/test_rls_backstop.py` records that as a
decision rather than an oversight, so a *new* untenanted table still fails by
name.

`alert_events` is ordinary tenant data with the standard one-predicate policy.

A single import contract keeps the separation from decaying:

> **Alerting is operator machinery, not a request path concern.**
> `api.routers` and `api.services` may not import `api.alerting`.

An alert that can be triggered by an HTTP request is a way for a customer to
page an operator.

## Running it

The `alerts` job is a Celery beat task on its own interval, separate from the
5-minute scheduler tick:

```
alerts    every 900s    ALERT_SCAN_SECONDS
```

It runs one operator pass and one customer pass on a single
`db.service_task()` connection — the service role, autocommit, the same
discipline as every other background job.

To watch it locally:

```
ALERT_WEBHOOK_URL=http://localhost:9000/hook ./scripts/worker.sh beat
```

A smoke pass over the real database, with nine of ten syncs failed, produces
exactly this and nothing else:

```
🔴 *sync_search_console is failing*
9 of 10 runs failed in the last day.
```

…silence on the next pass, and then (the three passes run back to back,
hence the duration):

```
🟢 *Recovered: sync_search_console is failing*
The last scan no longer sees this. It was open for less than a minute across 2 scan(s).
```

## Reading it

```sql
-- what is broken now
select kind, severity, title, occurrences, first_seen_at, notified_at
  from operator_alerts where resolved_at is null order by severity, first_seen_at;

-- anything opened but never delivered: alerting itself is broken
select * from operator_alerts
 where resolved_at is null and notified_at is null and first_seen_at < now() - interval '1 hour';

-- what a customer was told, and whether it arrived
select kind, title, fired_at, delivered_at, delivery_error
  from alert_events where website_id = %s order by fired_at desc;
```

The second query is the one to put on a dashboard. An open incident with no
`notified_at` means the destination is refusing us, which is the failure that
hides every other failure.

## Not yet

**Error tracking.** A crash in the request path is logged and nothing more.
That wants a real tracker — Sentry or equivalent, with a release and a stack
trace — rather than a table of our own; this file is about *conditions*
derived from the database, which is a different job from capturing exceptions.

**Acknowledgement.** There is no way to say "I know, I'm on it" and have the
6-hour re-notify stop. Adding it is a column and a line in `_notify`; it is
not here because one operator does not need it yet.

**Paging.** Everything is a webhook or an email. Severity `critical` is
recorded and rendered differently, but nothing wakes anybody up.
