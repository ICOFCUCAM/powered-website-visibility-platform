#!/usr/bin/env bash
# Everything CI would run, in the order that fails fastest.
set -euo pipefail
cd "$(dirname "$0")/.."
VENV="${VENV:-.venv}"

echo "── ruff ────────────────────────────────────────────"
"$VENV/bin/ruff" check forge tests
"$VENV/bin/ruff" format --check forge tests

echo "── import contracts ────────────────────────────────"
"$VENV/bin/lint-imports"

echo "── tests ───────────────────────────────────────────"
"$VENV/bin/python" -m pytest tests -q

echo
echo "all green"
