# 13 — The scheduler

Implements the V1 spec's six background jobs (§31) on the schedule and the
worker pools frozen in [08-architecture.md](08-architecture.md). Nothing here
changes that design; this document is what the implementation turned out to
need, and why.

The product's thesis is a loop — Discover → Diagnose → Recommend → Fix →
Measure → **Repeat** — and this is the Repeat. Without it every job is a
button press, and a customer who connects Google on Monday sees Monday's
numbers on Friday.

## The night

```
01:00  sync_search_console      04:00  calculate_scores
02:00  sync_analytics           04:30  generate_recommendations
03:00  crawl_website            Mon 06:00  generate_weekly_report
                                00:20  partitions, three months ahead
```

Ordered by their dependencies, which are real: scores need the crawl,
recommendations need the scores.

**Every website gets its own minute.** Firing the whole fleet at exactly 01:00
hands Google a thundering herd, collects a wall of 429s and spends the night's
quota on retries. Each website's slot is its base hour plus an offset derived
from its id. The 04:30 job is staggered across its own half hour so it cannot
spill past 05:00.

**The offset is a SHA-256 of the id, not `hash()`.** Python salts `hash()` per
process, so a website's slot would move every time a worker restarted — and
"did last night run?" would stop having an answer.

## Beat fires a tick, not the jobs

Celery beat runs one task every five minutes: `scheduler.tick`. The tick works
out what is due per website and claims it. That indirection buys two things a
cron entry per job cannot.

**A missed window is late, not lost.** The tick claims the most recent slot
that has PASSED, not the one the clock just struck. A pool that was down from
01:00 to 09:00 runs last night's sync at 09:05. Exactly one slot is ever
caught up, so a week's outage is not a week of backlog.

**New accounts need no special path.** A website connected at lunchtime
already has this morning's slot behind it, so its first sync starts on the
next tick. The spec's "new accounts run the same jobs immediately through the
queue" falls out of the same rule rather than needing one of its own.

## The claim is a unique index

`scheduled_runs` has `unique (website_id, job, window_start)`. Claiming a slot
IS inserting that row:

```sql
insert into scheduled_runs (...) values (...)
on conflict (website_id, job, window_start) do nothing
returning id
```

A row back means this process owns the slot; nothing back means another one
does. Two dispatchers racing on the same tick produce one winner and one
no-op. No advisory lock, no leader election, no Redis key that can expire
mid-crawl — the same discipline as `crawl_frontier`, where the database is the
only thing that has to be right.

**The claim must be committed before the task is queued.** This is the one
non-obvious constraint in the whole design, and it is invisible when it is
wrong. Enqueue inside the transaction that created the row and a worker can
pick the task up first, look up a row that does not exist yet, conclude
somebody else owns it, and return without doing the work — leaving the slot
claimed forever and the customer's data unrefreshed. Nothing errors and
nothing logs. So the dispatcher runs on an autocommit connection
(`db.service_task`), each claim commits on its own, and a tick is deliberately
not an atomic unit.

## Leases, because workers die

A run left `running` forever is worse than a failure, because a failure is
something an operator can see. Celery redelivers a task when its worker dies,
but `mark_running` refuses the second start — rightly, since a dead worker and
a slow one look identical from here.

So the tick also reaps. Anything `claimed` or `running` past a six-hour lease
becomes `failed`, with a reason that distinguishes the two cases:

| Left as | Reason | What it means on call |
| --- | --- | --- |
| `claimed` | `no worker picked this up` | The pool is down, or the queue is so far behind that the night ran out |
| `running` | `the worker did not report back` | A worker took it and died |

The lease is generous on purpose. Its only job is to tell "in flight" from
"abandoned", and the next night's slot is a fresh claim regardless, so nothing
waits on it to recover.

## Eligibility is checked before claiming

| Job | Runs when |
| --- | --- |
| `sync_search_console` | an active Search Console link exists |
| `sync_analytics` | an active Analytics link exists |
| `crawl_website` | `crawl_allowed` (decision 20 — and the crawler checks again before fetching) |
| everything else | at least one completed crawl |

Ineligible is a SKIP, not a failure. Nothing is wrong with a customer who has
not connected Analytics, and a nightly failed row every night would say there
was — burying the failures that matter.

## Autocommit, everywhere in a job

Background work runs on `db.service_task`, which is the service role without a
wrapping transaction. Two reasons, both learned the hard way:

- A 500-page crawl inside one transaction is a transaction open for minutes,
  holding back vacuum and accumulating locks the whole time.
- A failure rolls the whole thing back — **including the row the job was about
  to write to say that it failed**. A scheduler whose failure record
  disappears with the failure has no failure record.

## What an operator reads

`scheduled_runs` answers the question people actually ask, which is never "is
the cron running" but "did THIS customer's data refresh last night, and if
not, why not".

```sql
-- stuck
select * from scheduled_runs
 where status in ('claimed','running') and claimed_at < now() - interval '1 hour';

-- last night, by outcome
select job, status, count(*) from scheduled_runs
 where window_start > now() - interval '1 day' group by job, status;
```

It is tenant data with the same one-predicate RLS as everything else: readable
by its organisation, written only by the system — the same shape as
`llm_calls`, and for the same reason.

## Running it

```
./scripts/worker.sh beat        the clock
./scripts/worker.sh crawl       one pool, at its documented concurrency
./scripts/worker.sh all         every pool in one process, for development
```

One beat process per deployment. Two would double every tick; the slot claim
would still keep the work single, but there is no reason to make the database
referee it.

`SCHEDULER_TICK_SECONDS` shortens the tick for development. Production leaves
it alone: every slot is then claimed within five minutes of its due time,
which is close enough for a nightly schedule and rare enough that the tick
costs nothing.

**Run the pools apart in production.** A single worker across all queues is
how three hung Google syncs starve every score, plan and report behind them —
which is exactly what a smoke run of this scheduler did before the pools were
separated.

## Not yet

Alerting. A failed run is recorded and visible; nothing tells anyone. That is
the launch-readiness item ("crawl-failure and sync-failure alerting"), and it
wants a destination — email, Slack, a pager — more than it wants code.
