from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

TEST_JWT_SECRET = "test-secret-at-least-32-bytes-long-for-hs256"

os.environ.setdefault("JWT_SECRET", TEST_JWT_SECRET)
os.environ.setdefault("ENVIRONMENT", "test")

# Google credentials for the Hub. Fake values: every Google call in the test
# suite is served by api/tests/fake_google.py, so nothing leaves the process.
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault(
    "GOOGLE_REDIRECT_URI", "http://localhost:8000/api/v1/google/callback"
)
os.environ.setdefault("WEB_BASE_URL", "http://localhost:3000")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:56379/0")


def token_for(user_id: uuid.UUID, email: str = "user@example.com") -> str:
    import time

    return jwt.encode(
        {"sub": str(user_id), "email": email, "exp": int(time.time()) + 3600},
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


def auth_headers(user_id: uuid.UUID, email: str = "user@example.com") -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(user_id, email)}"}


@pytest.fixture(scope="session", autouse=True)
def token_master_key() -> str:
    """A throwaway master key for the vault, generated per test session."""
    from api.hub.services.keys import generate_master_key

    key = generate_master_key()
    os.environ["TOKEN_MASTER_KEY"] = key
    return key


@pytest.fixture(scope="session")
def database_url() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not set; run scripts/dev-db.sh first")
    return dsn


@pytest_asyncio.fixture
async def client(database_url: str) -> AsyncIterator[AsyncClient]:
    from api.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app), AsyncClient(
        transport=transport, base_url="http://test"
    ) as http_client:
        yield http_client


@pytest_asyncio.fixture
async def two_tenants(client: AsyncClient) -> dict[str, object]:
    """Two organisations that must never see each other.

    Seeded through the SERVICE role: creating organisations and users is an
    operator action, not something the API exposes in M1. It cannot be done as
    the request-path role, because RLS correctly refuses an insert for an
    organisation the caller is not yet a member of — which is itself evidence
    the policies are live.
    """
    import psycopg

    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    slug = uuid.uuid4().hex[:8]

    async with await psycopg.AsyncConnection.connect(
        os.environ.get("SERVICE_DATABASE_URL", os.environ["DATABASE_URL"]),
        autocommit=True,
    ) as conn:
        await conn.execute(
            "insert into organizations (id, name, slug) values (%s,%s,%s), (%s,%s,%s)",
            (org_a, "Org A", f"org-a-{slug}", org_b, "Org B", f"org-b-{slug}"),
        )
        await conn.execute(
            "insert into users (id, email) values (%s,%s), (%s,%s)",
            (user_a, f"a-{slug}@example.com", user_b, f"b-{slug}@example.com"),
        )
        await conn.execute(
            "insert into organization_members (organization_id, user_id, role) "
            "values (%s,%s,'owner'), (%s,%s,'owner')",
            (org_a, user_a, org_b, user_b),
        )

    return {
        "org_a": org_a,
        "org_b": org_b,
        "user_a": user_a,
        "user_b": user_b,
        "slug": slug,
    }


@pytest_asyncio.fixture
async def google(request):
    """Installs a fake Google for the duration of one test.

    The Hub's dependency layer takes an injected httpx client, so the real
    client code runs unchanged — only the transport is swapped.
    """
    from api.hub import deps as hub_deps
    from api.hub.services.oauth_state import InMemoryStateStore
    from api.tests.fake_google import FakeGoogle

    fake: FakeGoogle = getattr(request, "param", None) or FakeGoogle()
    hub_deps.set_http_client(fake.client())
    hub_deps.set_state_store(InMemoryStateStore())
    try:
        yield fake
    finally:
        hub_deps.set_http_client(None)
        hub_deps.set_state_store(None)


@pytest_asyncio.fixture
async def service_conn():
    """A service-role connection for assertions about stored state.

    Autocommit, and its own connection rather than `db.service_session()`:
    that helper holds a transaction open for its whole scope, so anything
    written through it would be invisible to the application's separate
    connection until the fixture exited — which reads as a bug in the code
    under test rather than in the harness.
    """
    import psycopg
    from psycopg.rows import dict_row

    async with await psycopg.AsyncConnection.connect(
        os.environ.get("SERVICE_DATABASE_URL", os.environ["DATABASE_URL"]),
        autocommit=True,
        row_factory=dict_row,
    ) as conn:
        yield conn
