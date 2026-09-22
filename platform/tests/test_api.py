"""The API's front door.

Exercised through the ASGI app without its lifespan, so these run with no
database: everything here is decided before a handler touches one.
"""

from __future__ import annotations

import os

import httpx
import pytest

TOKEN = "a-test-api-token"


@pytest.fixture(scope="module")
def app():
    os.environ.update(
        {
            "DATABASE_URL": "postgresql://unused/unused",
            "FORGE_MASTER_KEY": "unused",
            "FORGE_API_TOKEN": TOKEN,
            "FORGE_DEPLOY_DOMAIN": "deploys.example.com",
            "ENVIRONMENT": "development",
        }
    )
    from forge.config import get_settings
    from forge.main import app as application

    get_settings.cache_clear()
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://forge.test"
    ) as http:
        yield http


async def test_health_needs_no_token(client):
    response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.parametrize(
    "path", ["/projects", "/deployments/blog-abc12345", "/projects/blog/env"]
)
async def test_every_management_route_refuses_an_anonymous_caller(client, path):
    response = await client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_a_wrong_token_is_refused(client):
    response = await client.get(
        "/projects", headers={"Authorization": "Bearer not-the-token"}
    )
    assert response.status_code == 401


async def test_a_token_in_the_wrong_scheme_is_refused(client):
    """Basic auth carrying the right secret is still not a bearer token."""
    response = await client.get("/projects", headers={"Authorization": TOKEN})
    assert response.status_code == 401


async def test_errors_come_back_as_structured_json_not_a_stack_trace(client):
    response = await client.get("/projects")
    body = response.json()
    assert set(body["error"]) == {"code", "message"}
    assert "Traceback" not in response.text


async def test_the_webhook_secret_is_not_readable_through_the_api(client):
    """A read endpoint for it would mean the API token can retrieve every
    project's signing key."""
    response = await client.get("/webhooks/blog/secret")
    assert response.status_code == 404
    assert "not readable" in response.json()["error"]["message"]
