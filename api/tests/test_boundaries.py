"""Architectural boundaries that a linter cannot express.

The import contracts in pyproject.toml forbid Google SDKs outside the Hub. They
cannot see a hard-coded `googleapis.com` URL, which is the same violation
reached through httpx. This test can.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
API = ROOT / "api"
HUB = API / "hub"
TESTS = API / "tests"

_GOOGLE_HOST = re.compile(r"https?://[\w.-]*googleapis\.com|accounts\.google\.com")


def _python_files() -> list[pathlib.Path]:
    return [p for p in API.rglob("*.py") if TESTS not in p.parents]


def test_only_the_hub_names_a_google_url():
    """Covers endpoints and scope identifiers alike.

    Inside the Hub, endpoints live in `providers/` and scopes in
    `services/oauth_flow.py`; that split is the Hub's own business. What must
    never happen is either appearing outside `api/hub/`, because reaching
    Google through httpx from the core is the same boundary violation as
    importing the SDK — and the import contract cannot see it.
    """
    offenders = [
        p.relative_to(ROOT)
        for p in _python_files()
        if HUB not in p.parents
        and p.parent != HUB
        and _GOOGLE_HOST.search(p.read_text())
    ]
    assert offenders == [], f"a Google URL appears outside api/hub/: {offenders}"


def test_the_secrets_schema_is_only_touched_by_the_vault():
    """`secrets.oauth_tokens` holds refresh tokens. Exactly one module should
    know how to read it, so there is one place to audit."""
    vault = API / "hub" / "services" / "vault.py"
    offenders = [
        p.relative_to(ROOT)
        for p in _python_files()
        if p != vault and "secrets.oauth_tokens" in p.read_text()
    ]
    assert offenders == [], (
        f"secrets.oauth_tokens referenced outside the vault: {offenders}"
    )


def test_no_module_logs_a_token():
    """A token in a log line is a token in a log aggregator, a backup and a
    support screenshot (V1 spec §39)."""
    pattern = re.compile(
        r"(log(ger)?\.\w+|print)\([^)]*\b(access_token|refresh_token|id_token|"
        r"client_secret|code_verifier)\b",
        re.IGNORECASE,
    )
    offenders = [
        p.relative_to(ROOT) for p in _python_files() if pattern.search(p.read_text())
    ]
    assert offenders == [], f"a credential may be logged in: {offenders}"
