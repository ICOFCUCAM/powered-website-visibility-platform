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


def token_for(user_id: uuid.UUID, email: str = "user@example.com") -> str:
    import time

    return jwt.encode(
        {"sub": str(user_id), "email": email, "exp": int(time.time()) + 3600},
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


def auth_headers(user_id: uuid.UUID, email: str = "user@example.com") -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(user_id, email)}"}


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

    Seeded as the superuser because creating users and organisations is an
    operator action, not something the API exposes in M1.
    """
    import psycopg

    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    slug = uuid.uuid4().hex[:8]

    async with await psycopg.AsyncConnection.connect(
        os.environ["DATABASE_URL"], autocommit=True
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
