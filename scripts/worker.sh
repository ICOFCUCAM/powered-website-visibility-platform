#!/usr/bin/env bash
# Start a worker pool, or the beat scheduler.
#
#   ./scripts/worker.sh beat            the clock: fires a tick every 5 minutes
#   ./scripts/worker.sh crawl           one pool, at its own concurrency
#   ./scripts/worker.sh all             every pool in one process, for development
#
# The pools are separated by job shape (docs/08-architecture.md). A single
# queue is how one agency's forty-website backfill makes every other customer's
# dashboard look broken: crawls are long and host-rate-limited, syncs are
# quota-bound, analysis is short. Run them apart in production.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CELERY="${CELERY:-$ROOT/.venv/bin/celery}"
APP="api.workers.app:app"

declare -A CONCURRENCY=(
    [crawl]=8 [render]=2 [sync]=4 [analysis]=4 [ai]=2 [reports]=2
)

usage() { echo "usage: $0 {beat|all|${!CONCURRENCY[*]}}" >&2; exit 2; }
[ $# -eq 1 ] || usage

case "$1" in
    beat)
        # One beat process per deployment. Two would double every tick — the
        # slot claim in scheduled_runs would still keep the work single, but
        # there is no reason to make the database referee it.
        exec "$CELERY" -A "$APP" beat --loglevel INFO
        ;;
    all)
        queues="$(IFS=,; echo "${!CONCURRENCY[*]}")"
        exec "$CELERY" -A "$APP" worker -Q "$queues" -c 4 \
             --loglevel INFO -n dev@%h
        ;;
    *)
        pool="$1"
        [ -n "${CONCURRENCY[$pool]:-}" ] || usage
        exec "$CELERY" -A "$APP" worker -Q "$pool" -c "${CONCURRENCY[$pool]}" \
             --loglevel INFO -n "${pool}@%h"
        ;;
esac
