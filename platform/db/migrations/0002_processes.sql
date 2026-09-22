-- Processes: the things a project runs besides its website.
--
-- The deployment model does not change. A process is another container from
-- the same immutable image, which is what makes "the worker and the web app
-- are running the same code" true by construction rather than by discipline.
--
-- Workers and cron jobs run for the PRODUCTION deployment only. A preview of
-- a branch must not start a second consumer on the same queue, and must not
-- run the nightly billing job against real data because someone opened a pull
-- request. That rule lives in the engine; this schema only records intent.

BEGIN;

-- `web` is the routed process every project already has implicitly. It is in
-- the enum so that a project can eventually declare more than one, and so the
-- type column never needs a NULL meaning "the normal one".
CREATE TYPE process_type AS ENUM ('web', 'worker', 'cron');

CREATE TYPE job_status AS ENUM (
    'pending',
    'running',
    'succeeded',
    'failed',
    'timed_out',
    'skipped'
);

CREATE TABLE processes (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

    -- Part of the container name, so it obeys the same rules as a project slug.
    name        text         NOT NULL
                CHECK (name ~ '^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$'),
    type        process_type NOT NULL,

    -- NULL means the image's own CMD. Meaningful for `web`, rarely for the
    -- others — a worker that runs the web server is a mistake, not a default.
    command     text,

    -- Five-field cron, UTC. Required for `cron`, forbidden otherwise: a
    -- schedule on a long-running worker would be silently ignored, and a
    -- cron job without one would never run.
    schedule    text,

    -- Per process, because a queue consumer and a web server rarely want the
    -- same ceiling.
    memory_mb   integer      NOT NULL DEFAULT 512 CHECK (memory_mb >= 64),

    -- Long-running processes only. Cron jobs run one container per slot.
    replicas    integer      NOT NULL DEFAULT 1 CHECK (replicas BETWEEN 0 AND 20),

    -- A cron container still running when its timeout expires is killed and
    -- the run recorded as timed_out. Without this a job that hangs on a lock
    -- holds a slot forever and every later run is skipped.
    timeout_seconds integer  NOT NULL DEFAULT 900 CHECK (timeout_seconds > 0),

    -- Pausing without deleting. The obvious thing to reach for at 3am.
    enabled     boolean      NOT NULL DEFAULT true,

    created_at  timestamptz  NOT NULL DEFAULT now(),
    updated_at  timestamptz  NOT NULL DEFAULT now(),

    UNIQUE (project_id, name),

    CONSTRAINT processes_schedule_matches_type CHECK (
        (type = 'cron' AND schedule IS NOT NULL)
        OR (type <> 'cron' AND schedule IS NULL)
    )
);

CREATE INDEX processes_project_idx ON processes (project_id, type);

-- Enabled cron processes, which is the set the scheduler sweeps every tick.
CREATE INDEX processes_cron_idx ON processes (id) WHERE type = 'cron' AND enabled;

-- --------------------------------------------------------------------------
-- Job runs
-- --------------------------------------------------------------------------

CREATE TABLE job_runs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    process_id    uuid        NOT NULL REFERENCES processes(id) ON DELETE CASCADE,

    -- Which image actually ran. Nullable because a run can be recorded as
    -- skipped before any deployment was chosen.
    deployment_id uuid        REFERENCES deployments(id) ON DELETE SET NULL,

    -- The minute the schedule named, NOT the minute the job started. This is
    -- the whole idempotency mechanism: the unique constraint below means a
    -- slot can be claimed exactly once no matter how many workers sweep at
    -- the same moment, or how late they are.
    scheduled_for timestamptz NOT NULL,

    status        job_status  NOT NULL DEFAULT 'pending',
    exit_code     integer,

    -- Why a run was skipped, or how it failed, in one line.
    detail        text,

    -- The tail of the container's output. Bounded by the engine: a job that
    -- prints a megabyte a second should not be able to fill the database.
    output        text,

    container_id  text,

    created_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz,

    leased_by     text,
    leased_at     timestamptz,

    UNIQUE (process_id, scheduled_for)
);

-- The scheduler's claim scan.
CREATE INDEX job_runs_pending_idx
    ON job_runs (scheduled_for)
    WHERE status = 'pending';

-- The history shown on a process.
CREATE INDEX job_runs_recent_idx ON job_runs (process_id, scheduled_for DESC);

CREATE TRIGGER processes_touch BEFORE UPDATE ON processes
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

COMMIT;
