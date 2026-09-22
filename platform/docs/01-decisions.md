# Decisions

The choices that would be expensive to reverse, and why they went the way they
did. Written in the same spirit as the sibling project's `10-decisions.md`: the
reasoning matters more than the conclusion, because the conclusion is readable
from the code and the reasoning is not.

## Postgres is the queue

There is no Redis and no Celery.

The deployment table already has to be transactional and already holds the
state a broker would duplicate. A worker claims work with
`UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)`, which gives
exactly-once claiming across any number of workers, and a worker that dies
mid-transaction releases its lock instead of stranding the row.

The cost is polling — two seconds of latency on an idle queue. The thing
bought is that "which deployment is building" has exactly one answer, stored
once. A broker would give a second answer, and the two would eventually
disagree at the worst possible moment.

Abandoned work is recovered by a lease timestamp rather than a heartbeat
thread: the lease is pushed forward when a deploy *reaches the next phase*,
which is an honest signal that it is alive, where a timer only proves the
timer is alive.

## Production is a file, not a label

Traefik's Docker provider reads routes from container labels. That is how each
deployment gets its permanent hostname, and it is right for that — the
hostname is decided once and never changes.

It is wrong for production domains, because **Docker cannot change the labels
on a running container**. Promotion would mean `docker rm` and `docker run`:
a restart, a cold cache, and a few seconds of 502 every time production moves
— including during a rollback, which is the exact moment a site is least able
to afford more downtime.

So production lives in Traefik's file provider. Promotion writes one small
JSON file, atomically, and Traefik picks it up on its next watch tick with no
container touched. Rollback is the same write with an older service name.

The direct consequence: **rollback and promotion are one code path.** Rollback
is not an emergency procedure that gets exercised only during emergencies; it
is the code that runs on every successful deploy.

## One wildcard certificate, obtained over DNS-01

Every deployment gets a new hostname. Issuing a certificate per hostname is
the obvious implementation and it fails in about a fortnight: Let's Encrypt
allows 50 new certificates per registered domain per week, so roughly seven
deploys a day exhausts the allowance — and once exhausted, nothing gets a
certificate, including the custom domains serving real traffic.

One wildcard for `*.deploys.example.com`, set as the entrypoint's default,
has no such ceiling. Deployment routers therefore set `tls=true` and
deliberately **no** `certResolver`, so they inherit it.

Wildcards can only be proven over DNS-01, which is why a DNS provider token is
a hard requirement rather than a convenience. Customer domains are a different
shape — a handful, changing rarely — and get individual certificates over
HTTP-01.

## The Docker CLI, not the SDK

BuildKit. Cache mounts, secret mounts and `--progress=plain` are how builds
stay fast and how build-time secrets stay out of image layers, and the Python
SDK's build support predates all of it.

The side benefit is that every adapter function is a thin coroutine over a
subprocess with no hidden state, so the whole module is replaceable by a fake
in tests.

## Build secrets are mounted, never passed as ARG

`ARG` is the documented way to get a value into a build, and it is wrong for
secrets: build arguments are recorded in the image's metadata and
`docker history` prints them. A BuildKit secret mount exists for the duration
of one `RUN` and appears in no layer.

This has a visible consequence in the generated Dockerfiles — the build step
sources a file under `set -a` — and it is worth the ugliness.

## Two environment file formats

The build sources a shell file; the container is given `docker --env-file`.
These parse differently and the difference is not cosmetic:

- The shell file must be quoted and escaped. `PASSWORD=hunter2'; rm -rf /` is
  otherwise two commands.
- The env file must **not** be quoted. Docker splits on the first `=` and
  takes the rest literally, so quotes would end up inside the value.

The env file format cannot express a value containing a newline at all, which
matters because PEM keys are a common secret. Those are passed as `--env`
arguments instead. A single format for both would have silently corrupted one
of the two cases.

## Detection reads; it does not guess

The Next.js rules open `next.config` and look for `output` rather than
assuming. Guessing wrong there produces the worst available failure: an image
that builds cleanly, starts, and 404s on every asset, because `.next/standalone`
was never generated.

The config is parsed with a regex rather than executed, because a config file
is arbitrary JavaScript. The cost of the regex being fooled is a larger image;
the cost of executing the file is arbitrary code running in the control plane.

## Health is an HTTP response, not a running process

A container that is "running" has said only that its PID 1 has not exited. An
app crash-looping on a bad `DATABASE_URL` is running, most of the time.

So a deployment becomes ready when it answers HTTP on its port — **any**
status, including 404 and 500. The question is "is there a server here", not
"is the site correct": an API with no root route would otherwise never deploy,
and diagnosing a 500 is the owner's job.

The check talks to the container directly rather than through the router,
because going through the router asks a second question — "is the routing
right" — with a different fix.

## Production moves last

The order in the pipeline is: build, run, health-check, *then* promote. A
failed deploy cannot take a site down, because nothing was ever pointed at it.

Within promotion the order is equally deliberate: the container is proven to
serve before anything routes to it, and the database pointer moves after the
router file is written. A crash between those two leaves production serving
correctly from a deployment the database has not caught up to, which
reconciliation repairs. The other order would leave the database claiming a
promotion that never reached the router — a lie that nothing would correct.

## Reclaim containers, keep images

Twenty projects with six live deployments each is a hundred and twenty idle
containers holding RAM for URLs nobody has opened in weeks. Memory is the
resource that actually runs out on a single host.

So superseded deployments past `keep_warm` have their containers stopped and
removed, while their **images** stay. The deployment is still `ready`; it is
simply not resident, and a rollback restarts it in seconds rather than
rebuilding it. Production is never reclaimed, and that is enforced by passing
the protected set explicitly rather than by relying on "the newest N" — which
stops being the right set the moment someone rolls back.

## One user, one token

No accounts, no sessions, no login screen. The audience decision was made at
the start, and carrying multi-tenancy for a single-tenant platform would mean
paying for row-level security, quotas and build-sandbox isolation in every
query for a property nobody is using.

Adding accounts later means adding a table and changing `forge/deps.py`, and
nothing else: every route already asks for "the caller" rather than for a
token. The schema leaves room in the same way — nothing in it assumes a single
owner, it simply does not name one.

## The CLI talks to the database, not the API

The situation the CLI is most needed in is the one where the API will not
start, or where the broken deployment is the one serving the dashboard. A tool
that depends on the thing being repaired is no use during the repair.

## Workers and cron run for production only

A process is another container from the deployment's image. Which deployment
is not a free choice: a preview of a branch that started a second consumer on
the same queue would double-process every message, and a preview that ran the
nightly billing job would run it against real data because somebody opened a
pull request.

So `reconcile_workers` is called from promotion and from nowhere else, and a
job run resolves its image through the project's production pointer at fire
time. The rule is structural rather than a check that a future code path could
forget.

The same reasoning makes workers roll **back** with production. A rollback that
left the old workers running the new code would undo half the change, which is
worse than either version on its own.

## A cron job claims a slot; it does not fire on a timer

The scheduler's sweep does not run anything. It turns "this expression names
03:00 and it is now 03:00" into a row whose identity is
`(process_id, scheduled_for)`, and a unique constraint on that pair does the
rest of the work:

- Three workers sweeping in the same second produce one run.
- A sweep every twenty seconds against a minute-resolution schedule produces
  one run, not three.
- A worker that was down at 03:00 finds the slot still unclaimed when it comes
  back and runs the job **late rather than not at all**.

The catch-up is bounded — 25 hours — and deliberately returns only the most
recent missed slot. An hourly job whose worker was down for six hours runs
once, now, rather than replaying six slots against data that has already moved
past them. This is the same choice the sibling project's scheduler makes, for
the same reason.

A slot that is already known to be unrunnable — nothing is serving production
— is written in its final `skipped` state rather than inserted as `pending`
and then finished. Inserting it pending leaves a window in which the job loop
can claim and start a run the sweep is about to mark skipped; the outcome was
harmless, but it was a race that did not need to exist.

## Jobs run on a second loop, not the deploy loop

Deploys are strictly serial: a build saturates CPU and disk, and two at once
on a single host are slower than two in sequence while also able to starve each
other of memory.

Scheduled jobs are not like that. The worker process is only *waiting* on them
— the work happens inside another container — so they get their own loop and a
small concurrency limit. Sharing the deploy loop would mean a fifteen-minute
nightly job blocking every deploy for fifteen minutes, which is exactly the
kind of coupling that makes people stop using the scheduler.

## The cron parser is ours

Five fields, Vixie semantics, always UTC, about two hundred lines in the domain
layer. Not for lack of libraries, but because it is a small pure problem and
keeping it here means the scheduler's behaviour is testable without a clock, a
database or a container.

The part worth writing by hand is the one that is easy to get wrong:
day-of-month and day-of-week are the one field pair cron does **not** intersect.
`0 0 13 * fri` is "the 13th, and every Friday" — not "Friday the 13th". A
version that intersects them turns a job expected twice a month into one that
runs twice a year, and nothing announces it.

UTC always, with no per-project timezone. A schedule that means 03:00 in March
and 03:00 in October but with an hour of difference between them — or that
skips or repeats an hour twice a year — is not a property anyone wants in a job
that reconciles billing.

## Job output is captured; worker output is not

A job run keeps the tail of its container's output, bounded, in the database.
The tail rather than the head because the traceback is at the end, and bounded
because a job printing a megabyte a second should not be able to fill the disk
the platform's own database is on.

Workers get no such treatment: they are always on, so capturing their output
the same way would mean an unbounded, permanently-growing log table. Their
output stays in the container log, where Docker's rotation already applies.
