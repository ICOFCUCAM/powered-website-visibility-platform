-- Forge core schema.
--
-- The shape follows one rule: a deployment is immutable. Nothing about a
-- deployment is ever rewritten after it reaches a terminal state, because the
-- whole point of the platform is that "the thing that was serving at 14:02 on
-- Tuesday" is still there, still runnable, and still addressable by its own
-- URL. Promotion and rollback move a *pointer* (projects.production_deployment_id);
-- they never mutate the deployment being pointed at.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- --------------------------------------------------------------------------
-- Enumerations
-- --------------------------------------------------------------------------

-- The deployment lifecycle. Terminal states are READY, FAILED and CANCELLED;
-- everything else is in flight and belongs to exactly one worker.
--
--   queued -> building -> deploying -> ready
--        \-------\----------\-------> failed
--        \-------\----------\-------> cancelled
--
-- READY is not final in the sense of "forever serving": a ready deployment
-- whose container is later stopped to reclaim memory stays READY, with
-- container_id NULL. It can be woken again by a rollback, because the image
-- is what matters and the image is still in the registry.
CREATE TYPE deployment_status AS ENUM (
    'queued',
    'building',
    'deploying',
    'ready',
    'failed',
    'cancelled'
);

-- Why this deployment exists. Recorded because "who asked for this" is the
-- first question during an incident, and a git sha cannot answer it: the same
-- sha can arrive by push, by a manual redeploy and by a rollback.
CREATE TYPE deployment_trigger AS ENUM (
    'push',
    'manual',
    'rollback',
    'redeploy'
);

-- Which deployments a variable is visible to. Matching Vercel's split, because
-- the reason for it is real: a preview build must not hold the production
-- database password.
CREATE TYPE env_target AS ENUM ('production', 'preview', 'all');

-- Where a log line came from. Separated so a build failure can be shown
-- without the noise of the container's own stdout, and vice versa.
CREATE TYPE log_stream AS ENUM ('system', 'build', 'run');

-- --------------------------------------------------------------------------
-- Projects
-- --------------------------------------------------------------------------

CREATE TABLE projects (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- The slug is the project's identity in every URL it will ever have, so it
    -- is immutable in practice even though nothing here forbids an update.
    slug                    text        NOT NULL UNIQUE
                            CHECK (slug ~ '^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$'),
    name                    text        NOT NULL,

    repo_url                text        NOT NULL,
    production_branch       text        NOT NULL DEFAULT 'main',

    -- The subdirectory the app actually lives in. This field exists because of
    -- a real outage in the sibling project: a repository root holding both an
    -- `api/` and a `web/` was deployed whole, the host's framework detection
    -- found the wrong one, and every route returned 500. Naming the directory
    -- is how that becomes impossible rather than merely fixed.
    root_directory          text        NOT NULL DEFAULT '',

    -- All four are overrides. NULL means "let detection decide", which is the
    -- normal case; a value means the repository said otherwise and detection
    -- must not argue.
    framework               text,
    install_command         text,
    build_command           text,
    start_command           text,

    -- The port the container listens on. NULL means detection picks it, and
    -- whatever it picks is passed to the container as $PORT regardless, so an
    -- app that reads $PORT is always right.
    port                    integer     CHECK (port IS NULL OR (port > 0 AND port < 65536)),

    -- Resource ceilings, applied at `docker run`. A build that leaks memory
    -- should lose its own container, not the host that every other site is on.
    memory_mb               integer     NOT NULL DEFAULT 512 CHECK (memory_mb >= 64),
    cpu_shares              numeric(4,2) NOT NULL DEFAULT 1.0 CHECK (cpu_shares > 0),

    -- How many superseded READY deployments keep their containers running so a
    -- rollback is instant. Older ones keep their image and are restarted on
    -- demand, which costs seconds rather than memory.
    keep_warm               integer     NOT NULL DEFAULT 2 CHECK (keep_warm >= 0),

    -- The pointer that promotion and rollback move. Deliberately nullable: a
    -- project exists before its first successful deployment.
    production_deployment_id uuid,

    -- Shared secret for this project's inbound git webhook. Per project, not
    -- global, so rotating one repository's hook cannot break the others.
    webhook_secret          text        NOT NULL,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- Deployments
-- --------------------------------------------------------------------------

CREATE TABLE deployments (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      uuid        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

    -- The deployment's own permanent hostname label, e.g. `blog-3f9a2c71`.
    -- Globally unique because it is a DNS label under the wildcard domain.
    short_id        text        NOT NULL UNIQUE
                    CHECK (short_id ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'),

    -- Monotonic per project, for display: "deployment #14".
    number          integer     NOT NULL,

    status          deployment_status  NOT NULL DEFAULT 'queued',
    trigger         deployment_trigger NOT NULL,

    git_sha         text        NOT NULL CHECK (git_sha ~ '^[0-9a-f]{40}$'),
    git_ref         text        NOT NULL,
    git_message     text,
    git_author      text,

    -- What detection concluded, frozen at build time. Kept even when the
    -- project later overrides it, because this deployment was built the old
    -- way and the record should say so.
    framework       text,
    image_tag       text,
    internal_port   integer     CHECK (internal_port IS NULL
                                       OR (internal_port > 0 AND internal_port < 65536)),

    -- NULL once the container is reclaimed. The deployment stays READY.
    container_id    text,

    -- The failure, in one line, for a list view. The detail is in the logs.
    error           text,

    -- A rollback records where it came from, so the history reads as a story
    -- rather than as an unexplained repeat of an old sha.
    rolled_back_from uuid       REFERENCES deployments(id) ON DELETE SET NULL,

    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    built_at        timestamptz,
    ready_at        timestamptz,
    finished_at     timestamptz,

    -- Worker lease. Held while the deployment is in flight so a second worker
    -- cannot pick it up, and stamped with a time so a worker that died does
    -- not strand the row forever.
    leased_by       text,
    leased_at       timestamptz,

    UNIQUE (project_id, number)
);

ALTER TABLE projects
    ADD CONSTRAINT projects_production_deployment_fk
    FOREIGN KEY (production_deployment_id)
    REFERENCES deployments(id) ON DELETE SET NULL;

-- The queue scan. Partial, because the queue is the only part of this table
-- that is ever hot, and it is almost always empty.
CREATE INDEX deployments_queued_idx
    ON deployments (created_at)
    WHERE status = 'queued';

-- The two list views the UI actually asks for.
CREATE INDEX deployments_project_recent_idx
    ON deployments (project_id, number DESC);

-- Finding the live containers at startup, to reconcile against Docker.
CREATE INDEX deployments_running_idx
    ON deployments (project_id)
    WHERE container_id IS NOT NULL;

-- A deployment in flight must hold a lease; a queued one must not. This is the
-- invariant the worker relies on, so the database enforces it rather than
-- trusting every future code path to remember.
ALTER TABLE deployments ADD CONSTRAINT deployments_lease_matches_status CHECK (
    (status IN ('building', 'deploying') AND leased_by IS NOT NULL)
    OR (status NOT IN ('building', 'deploying'))
);

-- --------------------------------------------------------------------------
-- Logs
-- --------------------------------------------------------------------------

CREATE TABLE deployment_logs (
    id            bigserial PRIMARY KEY,
    deployment_id uuid        NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
    seq           integer     NOT NULL,
    stream        log_stream  NOT NULL,
    line          text        NOT NULL,
    at            timestamptz NOT NULL DEFAULT now(),

    UNIQUE (deployment_id, seq)
);

-- Log tailing reads forward from a cursor, which is exactly this index.
CREATE INDEX deployment_logs_tail_idx ON deployment_logs (deployment_id, seq);

-- --------------------------------------------------------------------------
-- Environment variables
-- --------------------------------------------------------------------------

CREATE TABLE env_vars (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    uuid        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key           text        NOT NULL CHECK (key ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    target        env_target  NOT NULL DEFAULT 'all',

    -- Encrypted with the platform's master key before it arrives here. The
    -- database never sees a plaintext secret, so a leaked backup is not a
    -- leaked credential.
    value_encrypted bytea     NOT NULL,

    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),

    UNIQUE (project_id, key, target)
);

-- --------------------------------------------------------------------------
-- Custom domains
-- --------------------------------------------------------------------------

CREATE TABLE domains (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    host        text        NOT NULL UNIQUE CHECK (host = lower(host)),

    -- A domain routes to production only once DNS has been seen pointing here.
    -- Until then it is configuration, not a route: attaching an unverified
    -- host to the router would ask the ACME provider for a certificate that
    -- cannot be issued, and rate limits are not forgiving.
    verified_at timestamptz,

    -- Exactly one primary per project; the others redirect to it.
    is_primary  boolean     NOT NULL DEFAULT false,

    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX domains_one_primary_idx
    ON domains (project_id)
    WHERE is_primary;

-- --------------------------------------------------------------------------
-- updated_at maintenance
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER projects_touch BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TRIGGER env_vars_touch BEFORE UPDATE ON env_vars
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

COMMIT;
