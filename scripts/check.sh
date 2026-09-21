#!/usr/bin/env bash
# Everything CI runs. Requires DATABASE_URL and JWT_SECRET.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> ruff";          .venv/bin/ruff check api/
echo "==> import contracts"; .venv/bin/lint-imports
echo "==> pytest";        .venv/bin/pytest -q
# Schema tests seed organisations and switch roles, which is operator work:
# they run as the owner, not as the RLS-bound request-path role.
echo "==> schema"
psql "${ADMIN_DATABASE_URL:-$DATABASE_URL}" -v ON_ERROR_STOP=1 \
     -f db/tests/smoke.sql 2>&1 | grep -E 'PASS|ERROR'
echo "==> web typecheck"; (cd web && npx tsc --noEmit)
echo "==> ok"
