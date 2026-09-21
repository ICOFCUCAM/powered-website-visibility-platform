#!/usr/bin/env bash
# Starts a throwaway local Postgres, applies every migration and the roles,
# and prints the DSN. Used by the test suite and for local development.
#
#   eval "$(./scripts/dev-db.sh)"      # exports DATABASE_URL
set -euo pipefail

PGBIN="${PGBIN:-/usr/lib/postgresql/16/bin}"
DIR="${DEV_DB_DIR:-/var/lib/postgresql/ck}"
PORT="${DEV_DB_PORT:-55432}"
DB="${DEV_DB_NAME:-viz}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$PGBIN:$PATH"

log() { echo "$@" >&2; }

if ! pg_isready -h "$DIR" -p "$PORT" >/dev/null 2>&1; then
    log "==> starting postgres in $DIR"
    rm -rf "$DIR"; mkdir -p "$DIR"; chown postgres "$DIR"
    su postgres -c "PATH=$PGBIN:\$PATH initdb -D $DIR/data -A trust -U postgres" >/dev/null
    su postgres -c "PATH=$PGBIN:\$PATH pg_ctl -D $DIR/data \
        -o '-k $DIR -p $PORT -c listen_addresses=' -l $DIR/pg.log start -w" >/dev/null
fi

psql -h "$DIR" -p "$PORT" -U postgres -q \
     -c "drop database if exists $DB;" -c "create database $DB;" >/dev/null

for f in "$ROOT"/db/migrations/*.sql; do
    log "==> $(basename "$f")"
    # stdout is the DSN channel for `eval`, so result rows go to /dev/null.
    psql -h "$DIR" -p "$PORT" -U postgres -d "$DB" -v ON_ERROR_STOP=1 -q -f "$f" >/dev/null
done
log "==> roles"
psql -h "$DIR" -p "$PORT" -U postgres -d "$DB" -v ON_ERROR_STOP=1 -q -f "$ROOT/db/roles.sql" >/dev/null

echo "export DATABASE_URL='postgresql://postgres@/${DB}?host=${DIR}&port=${PORT}'"
