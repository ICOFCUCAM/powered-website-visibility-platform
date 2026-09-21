#!/usr/bin/env bash
# Everything CI runs. Requires DATABASE_URL and JWT_SECRET.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> ruff";          .venv/bin/ruff check api/
echo "==> import contracts"; .venv/bin/lint-imports
echo "==> pytest";        .venv/bin/pytest -q
# Schema tests seed organisations and switch roles, which is operator work:
# they run as the owner, not as the RLS-bound request-path role. Falling back
# to DATABASE_URL would fail with a confusing row-level-security error instead
# of saying what is actually missing.
echo "==> schema"
if [ -z "${ADMIN_DATABASE_URL:-}" ]; then
    echo "    ADMIN_DATABASE_URL is not set." >&2
    echo "    Schema tests need the owner role. Run: eval \"\$(./scripts/dev-db.sh)\"" >&2
    exit 2
fi
psql "$ADMIN_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/tests/smoke.sql 2>&1 |
    grep -E 'PASS|ERROR'
echo "==> web typecheck"; (cd web && npx tsc --noEmit)
echo "==> web tests";     (cd web && npx vitest run --reporter=dot)
echo "==> ok"
