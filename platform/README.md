# Forge

A self-hosted deployment platform. Push to a branch; a container is built,
started, health-checked and routed, on hardware you own.

```
git push  →  clone  →  detect  →  docker build  →  run  →  health check  →  route
                                                                              │
                         every deployment keeps its own permanent URL  ───────┤
                         production is a pointer you can move in a second ────┘
```

It is built for the case Vercel is bad at and charges for: long-running
servers, background processes, APIs, and sites whose bill should be a fixed
monthly number rather than a function of traffic.

## Status

**The engine is complete and tested; it has not yet been run end to end
against a live Docker daemon.** 102 tests cover framework detection, image
generation, secret handling, router configuration, encryption, webhook
signatures and API authentication. The parts that need a daemon — an actual
`docker build`, an actual promotion — are written but unexercised. See
[What is proven and what is not](#what-is-proven-and-what-is-not).

The dashboard is server-rendered from the control plane itself — no build
step, no separate deployment, and every action on it is a plain form that
works without JavaScript. That is deliberate: it is the page you open when a
deploy has gone wrong, so it must not depend on anything that could be wrong
at the same time.

## What it does

| | |
| --- | --- |
| **Deploys anything containerisable** | Next.js, Astro, Nuxt, SvelteKit, Remix, Vite, Django, FastAPI, Flask, Go, static — or your own `Dockerfile`, which is always honoured |
| **A permanent URL per deployment** | `blog-3f9a2c71.deploys.example.com` keeps working after twenty more deploys |
| **Promotion is a pointer** | Production is a file the router watches. Moving it touches no container |
| **Rollback in about a second** | The same operation as promotion, run against an older deployment |
| **Encrypted environment variables** | Fernet at rest, scoped to production or preview, never readable back through the API |
| **Custom domains with automatic TLS** | With a canonical redirect from every alias to the primary |
| **Preview deploys** | Any non-production branch gets a URL and is kept away from production secrets |
| **A dashboard** | Projects, deployments, live build logs, variables, domains and one-click rollback |

## How it works

Four processes and the containers they create.

```
                    :80 :443
                       │
                ┌──────▼───────┐        reads container labels
                │   Traefik    │◄───────────────────────────────┐
                │  (the edge)  │                                │
                └──┬────────┬──┘                                │
       watches ────┘        └──── routes to ──┐                 │
            │                                 │                 │
   ┌────────▼─────────┐          ┌────────────▼──────────────┐  │
   │ router config    │          │  blog-3f9a2c71  (live)    │──┘
   │ project-blog.json│          │  blog-9d1e0f44  (warm)    │
   └────────▲─────────┘          │  api-77c2b310   (live)    │
            │ writes             └───────────────────────────┘
            │                                 ▲
   ┌────────┴──────┐   claims    ┌────────────┴──────┐
   │  control API  │◄────────────┤   deploy worker   │ builds & runs
   └───────┬───────┘  deployments└─────────┬─────────┘
           │                               │
           └────────► Postgres ◄────────────┘
```

**The API** takes requests and queues work. **The worker** claims one
deployment at a time (`FOR UPDATE SKIP LOCKED`, so a second worker is safe to
add) and runs it to completion. **Postgres** is the queue — there is no broker,
because the state is already in a transactional store and a second system
holding "which deployment is building" is a second thing to disagree.

**Traefik** routes by two independent mechanisms, and the split matters:

- A deployment's own permanent hostname is a **container label**, set once at
  `docker run` and never touched again.
- Production domains are a **file** the router watches.

Docker cannot change a running container's labels. If production lived in
labels, promoting would mean destroying and recreating the container — a
restart, a cold cache and a few seconds of 502 on every promotion and every
rollback. Writing a small JSON file instead makes promotion atomic and
instant, and makes rollback the identical operation.

### The deployment lifecycle

```
queued ──► building ──► deploying ──► ready
   └───────────┴────────────┴───────► failed
```

Production moves **last**, only after the new container has answered a real
HTTP request. A failed deploy cannot take a site down, because nothing was
ever pointed at it.

A deployment that succeeds is immutable. Superseded ones keep their images and
have their containers reclaimed after `keep_warm`, so rolling back is a
restart rather than a rebuild.

## Setting it up

You need a host with Docker, a domain, and a DNS provider with an API token.

**1. DNS.** Point a wildcard at the host:

```
*.deploys.example.com.   A   203.0.113.10
 deploys.example.com.    A   203.0.113.10
```

**2. Configure.**

```bash
cp .env.example .env
```

Fill in `FORGE_DEPLOY_DOMAIN`, `FORGE_ACME_EMAIL`, `FORGE_DNS_PROVIDER` and
its API token, then generate the three secrets:

```bash
openssl rand -hex 32                                 # FORGE_API_TOKEN
openssl rand -hex 24                                 # POSTGRES_PASSWORD
docker compose run --rm --no-deps api forge keygen   # FORGE_MASTER_KEY
```

Keep a backup of `FORGE_MASTER_KEY`. Losing it makes every stored environment
variable unreadable, and changing it has exactly the same effect.

**3. Start, and create the schema.**

```bash
docker compose up -d
docker compose exec api forge migrate
docker compose exec api forge doctor
```

`doctor` checks the things that are actually wrong when nothing deploys: the
Docker socket, the writable volumes, whether the wildcard resolves, and
whether TLS is configured at all.

### Why a DNS API token is required

Every deployment invents a new hostname. If each one asked for its own
certificate, Let's Encrypt's limit of **50 new certificates per registered
domain per week** would be spent by about seven deploys a day — and then
nothing would get a certificate, including the custom domains carrying real
traffic.

So Forge issues **one wildcard certificate** for `*.deploys.example.com`, and
every deployment router inherits it. A wildcard can only be proven over a
DNS-01 challenge, which is why a DNS provider token is not optional. Customer
domains are separate and low-volume: they get individual certificates over
HTTP-01.

## Your first deploy

```bash
forge project create --name "Blog" --repo https://github.com/you/blog.git
forge deploy blog
forge logs blog-3f9a2c71 --follow
```

```
·· deploying Blog #1 — main at 4f2a9c1e (manual)
·· cloning https://github.com/you/blog.git at 4f2a9c1e
·· Next.js with output: 'standalone' — serving the traced server bundle
·· building image forge/blog:4f2a9c1e88b1
   #8 [build 4/4] RUN npm run build
   …
·· image built
·· starting container on port 8080 with 3 environment variables
·· healthy after 1.4s (4 attempts) — answered 200 on /
·· ready at https://blog-3f9a2c71.deploys.example.com
·· no verified custom domains — serving on https://blog-3f9a2c71.deploys.example.com
```

Then deploy on every push:

```bash
forge webhook blog      # prints the payload URL and the secret
```

The same thing is on the project page in the dashboard, at
`https://forge.deploys.example.com` — sign in with `FORGE_API_TOKEN` and the
browser holds a signed cookie derived from it, so there is still only one
credential to keep.

Paste both into the repository's **Settings → Webhooks**. Pushes to the
production branch deploy and promote; pushes to any other branch get a preview
URL and cannot see production-scoped variables.

### Environment variables

```bash
forge env set blog DATABASE_URL 'postgres://…' --target production
forge env set blog STRIPE_KEY - < key.txt        # `-` reads stdin, staying
                                                  # out of your shell history
```

They are applied at **build** time as well as run time, so changing one takes
effect on the next build:

```bash
forge deploy blog
```

### Custom domains

```bash
forge domain add blog example.com --primary
forge domain add blog www.example.com
# point DNS at deploys.example.com, then:
forge domain verify blog example.com
```

Verification is a DNS check before the hostname reaches the router — an
unverified name would fail its ACME challenge against a rate limit shared by
every site on the host. Once verified, aliases 301 to the primary, because two
hostnames serving identical pages is a duplicate-content problem.

### Rollback

```bash
forge deployments blog
#  * #14   ready      live  9c8b1a22 main    blog-7e1a0d93
#    #13   ready      live  4f2a9c1e main    blog-3f9a2c71
#    #12   failed           2b7d4f01 main    blog-11c9e2a7

forge promote blog '#13'
```

## Reference

### API

The dashboard owns the root path; the JSON API lives under `/api`. All of it
except `/health`, `/ready` and `/webhooks/*` needs
`Authorization: Bearer $FORGE_API_TOKEN` — or the dashboard's session cookie,
which is derived from the same token so a browser needs no second credential.

| | |
| --- | --- |
| `POST /api/projects` `GET /api/projects` | create and list |
| `GET PATCH DELETE /api/projects/{ref}` | `{ref}` is a slug or a uuid |
| `POST /api/projects/{ref}/deploy` | queue a deployment |
| `GET /api/projects/{ref}/deployments` | history |
| `GET PUT /api/projects/{ref}/env` · `DELETE …/env/{key}` | variables; values never come back out |
| `GET POST /api/projects/{ref}/domains` · `POST …/{host}/verify` | custom domains |
| `GET /api/deployments/{id}` | one deployment |
| `GET /api/deployments/{id}/logs` · `/logs/stream` | paged, or server-sent events |
| `POST /api/deployments/{id}/promote` | promote or roll back |
| `POST /api/deployments/{id}/redeploy` | rebuild the same commit |
| `POST /api/deployments/{id}/cancel` | only while still queued |
| `POST /webhooks/{slug}` | git push, HMAC-signed |

### Telling Forge how to build

Detection runs most-explicit-first: a `Dockerfile`, then a `forge.json`, then
project settings, then framework signatures, then a bare `index.html`.

```json
{
  "framework": "next",
  "installCommand": "npm ci --legacy-peer-deps",
  "buildCommand": "build:production",
  "startCommand": "node dist/server.js",
  "port": 3000
}
```

Committing a `Dockerfile` always wins, and is how you deploy a language Forge
has no rule for. Its `EXPOSE` is read for the port.

### What Forge tells your app

`PORT`, `FORGE_URL`, `FORGE_DEPLOYMENT`, `FORGE_GIT_SHA`, `FORGE_ENV`
(`production` or `preview`). `FORGE_URL` is how a preview build discovers the
hostname it cannot know at commit time — for canonical tags, OAuth redirects
and `og:image`.

## Security

- **Build secrets are mounted, never baked.** Build-time variables arrive
  through a BuildKit secret mount, which exists for one `RUN` and is in no
  layer. `ARG` would put every one of them in `docker history`.
- **Every generated image drops out of root**, and containers run with
  `--cap-drop=ALL`, `--security-opt=no-new-privileges`, a memory ceiling, a
  pids limit and capped logs.
- **No deployment publishes a host port.** Containers are reachable only
  through the router, on a shared private network.
- **Repository URLs are an allowlist.** `ext::` URLs make `git clone` execute
  a shell command; they are refused before git sees them.
- **Secrets compare in constant time**, both the API token and webhook
  signatures.
- **The control plane holds the Docker socket, which is equivalent to root on
  the host.** That is inherent to the design. Do not run anything else you
  don't trust on this machine, and do not expose the API without TLS.

## What is proven and what is not

Run `./scripts/check.sh` for ruff, the import contracts and the suite.

**Tested (120 tests, no daemon needed):** detection across nine stacks and its
tie-breaks, including that a commented-out `output: 'standalone'` is not read
as enabled; image invariants over every generator (non-root, multi-stage, no
`ARG`, dependency layer before source); DNS label safety and non-enumerable
deployment ids; shell quoting proved by round-tripping through a real `sh`;
multi-line values kept out of the env file; `0600` on both secret files;
router JSON including priority, canonical redirect and path preservation;
encryption round-trip and loud failure on a rotated key; webhook signature
rejection and push filtering; API auth on every management route.

**Not yet exercised:** a real `docker build`, container start, health check,
promotion or rollback against a live daemon — this environment has the Docker
CLI but no daemon. Also unexercised: the migrations against a real Postgres,
and Traefik actually reloading a written route file.

**Not built:** per-project cron jobs and background workers; image garbage
collection; log retention; metrics; multi-node scheduling.

## Layout

```
forge/
  domain/        pure: detection, Dockerfile generation, naming, models
  adapters/      Postgres, Docker CLI, git, Traefik files, encryption
  repositories/  queries, including the deployment queue
  engine/        the pipeline, promotion, health, environment, routing
  routers/       HTTP (the JSON API)
  web/           the dashboard: routes, templates, one stylesheet, one script
  worker.py      the deploy loop
  cli.py         the operator's tool
db/migrations/   schema
```

Three import contracts are enforced in `scripts/check.sh`: the domain layer
imports no vendor, only the engine drives Docker and git, and the layers run
one way. They fail the build rather than waiting for a reviewer.
