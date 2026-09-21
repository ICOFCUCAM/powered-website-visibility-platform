#!/usr/bin/env bash
# Applies every migration to a scratch database and runs the smoke tests.
# This is the CI check described in docs/08-structure.md: it catches invalid
# DDL before it reaches an environment that has data in it.
#
#   DB=postgres://user:pass@host/postgres ./db/tests/run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DB="${DB:-postgres://postgres@localhost/postgres}"
SCRATCH="${SCRATCH_DB:-viz_test}"

echo "==> recreating $SCRATCH"
psql "$DB" -q -c "drop database if exists $SCRATCH;" -c "create database $SCRATCH;"

TARGET="${DB%/*}/$SCRATCH"

for f in "$ROOT"/db/migrations/*.sql; do
    echo "==> $(basename "$f")"
    psql "$TARGET" -v ON_ERROR_STOP=1 -q -f "$f"
done

echo "==> roles"
psql "$TARGET" -v ON_ERROR_STOP=1 -q -f "$ROOT/db/roles.sql"

echo "==> smoke tests"
psql "$TARGET" -v ON_ERROR_STOP=1 -f "$ROOT/db/tests/smoke.sql" 2>&1 | grep -E 'NOTICE|ERROR'

echo "==> ok"
