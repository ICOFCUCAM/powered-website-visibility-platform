"""The dashboard renders, and refuses to render for a stranger."""

from __future__ import annotations

import os

import httpx
import pytest

from forge.domain import session
from forge.domain.models import DeploymentStatus, LogStream
from tests import fakes

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
def repos(monkeypatch):
    """Serve the whole dashboard from fixtures instead of Postgres."""
    live = fakes.deployment()
    proj = fakes.project(production_deployment_id=live.id)
    state = {
        "projects": [proj],
        "deployments": [
            live,
            fakes.deployment(
                number=13,
                short_id="blog-9d1e0f44",
                status=DeploymentStatus.FAILED,
                error="The container never became healthy: it exited before it "
                "served a request",
            ),
            fakes.deployment(
                number=12,
                short_id="blog-11c9e2a7",
                status=DeploymentStatus.BUILDING,
                container_id=None,
            ),
        ],
        "domains": [
            fakes.domain("example.com", verified=True, primary=True),
            fakes.domain("www.example.com", verified=False, primary=False),
        ],
        "env": [fakes.env_var("DATABASE_URL"), fakes.env_var("STRIPE_KEY")],
        "processes": [fakes.process(), fakes.cron()],
        "runs": [
            fakes.job_run(),
            fakes.job_run(
                status=fakes.JobStatus.FAILED,
                exit_code=1,
                detail="Exited 1",
                output="Error: connection refused",
            ),
        ],
        "logs": [
            fakes.log_line(
                1, "deploying Blog #14 — main at 4f2a9c1e (push)", LogStream.SYSTEM
            ),
            fakes.log_line(
                2,
                "Next.js with output: 'standalone' — serving the traced server bundle",
                LogStream.SYSTEM,
            ),
            fakes.log_line(3, "#8 [build 4/4] RUN npm run build"),
            fakes.log_line(
                4, "healthy after 1.4s (4 attempts) — answered 200 on /", LogStream.SYSTEM
            ),
        ],
    }

    async def by_slug(slug):
        return state["projects"][0]

    async def by_id(_id):
        return state["projects"][0]

    async def dep_get(_id):
        return state["deployments"][0]

    async def dep_by_short(short_id):
        for d in state["deployments"]:
            if d.short_id == short_id:
                return d
        raise AssertionError(short_id)

    monkeypatch.setattr(
        "forge.web.routes.project_repo.list_all", lambda: _async(state["projects"])
    )
    monkeypatch.setattr("forge.web.routes.project_repo.get_by_slug", by_slug)
    monkeypatch.setattr("forge.web.routes.project_repo.get", by_id)
    monkeypatch.setattr(
        "forge.web.routes.project_repo.list_domains", lambda _id: _async(state["domains"])
    )
    monkeypatch.setattr(
        "forge.web.routes.project_repo.list_env", lambda _id: _async(state["env"])
    )
    monkeypatch.setattr("forge.web.routes.deployment_repo.get", dep_get)
    monkeypatch.setattr("forge.web.routes.deployment_repo.get_by_short_id", dep_by_short)
    monkeypatch.setattr(
        "forge.web.routes.deployment_repo.list_for_project",
        lambda _id, limit=25: _async(state["deployments"]),
    )
    monkeypatch.setattr(
        "forge.web.routes.deployment_repo.read_logs",
        lambda _id, limit=0, after=0: _async(state["logs"]),
    )

    async def process_by_name(_project_id, name):
        for proc in state["processes"]:
            if proc.name == name:
                return proc
        raise AssertionError(name)

    monkeypatch.setattr(
        "forge.web.routes.process_repo.list_for_project",
        lambda _id: _async(state["processes"]),
    )
    monkeypatch.setattr("forge.web.routes.process_repo.get_by_name", process_by_name)
    monkeypatch.setattr(
        "forge.web.routes.process_repo.list_runs",
        lambda _id, limit=25: _async(state["runs"]),
    )
    monkeypatch.setattr(
        "forge.web.routes.process_repo.last_run", lambda _id: _async(state["runs"][0])
    )
    return state


async def _async(value):
    return value


@pytest.fixture
def anon(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://forge.test", follow_redirects=False
    )


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    cookie = session.issue(token=TOKEN)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://forge.test",
        cookies={session.COOKIE_NAME: cookie},
        follow_redirects=False,
    ) as http:
        yield http


class TestAccess:
    @pytest.mark.parametrize(
        "path", ["/", "/projects/blog", "/projects/new", "/deployments/blog-3f9a2c71"]
    )
    async def test_a_stranger_is_sent_to_sign_in_not_given_json(self, anon, path):
        async with anon as http:
            response = await http.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    async def test_the_sign_in_page_is_public(self, anon):
        async with anon as http:
            response = await http.get("/login")
        assert response.status_code == 200
        assert "API token" in response.text

    async def test_a_wrong_token_does_not_set_a_cookie(self, anon):
        async with anon as http:
            response = await http.post("/login", data={"token": "wrong"})
        assert response.status_code == 303
        assert "/login" in response.headers["location"]
        assert session.COOKIE_NAME not in response.cookies

    async def test_signing_in_sets_an_httponly_cookie(self, anon):
        async with anon as http:
            response = await http.post("/login", data={"token": TOKEN})
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert "httponly" in response.headers["set-cookie"].lower()

    async def test_a_tampered_cookie_is_rejected(self, app):
        transport = httpx.ASGITransport(app=app)
        forged = session.issue(token="some-other-token")
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://forge.test",
            cookies={session.COOKIE_NAME: forged},
            follow_redirects=False,
        ) as http:
            response = await http.get("/")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestPages:
    async def test_the_project_list_shows_what_is_serving(self, client, repos):
        response = await client.get("/")
        assert response.status_code == 200
        assert "Blog" in response.text
        assert "example.com" in response.text

    async def test_the_project_page_shows_deployments_domains_and_variables(
        self, client, repos
    ):
        response = await client.get("/projects/blog")
        assert response.status_code == 200
        body = response.text
        assert "#14" in body and "#13" in body
        assert "DATABASE_URL" in body and "STRIPE_KEY" in body
        assert "www.example.com" in body
        assert "needs DNS" in body

    async def test_a_variables_value_is_never_rendered(self, client, repos):
        """The page lists keys and scopes. The ciphertext does not reach the
        template and the plaintext does not exist outside a deploy."""
        response = await client.get("/projects/blog")
        assert "ciphertext" not in response.text

    async def test_the_webhook_secret_is_masked_until_asked_for(self, client, repos):
        response = await client.get("/projects/blog")
        assert "••••" in response.text

    async def test_the_deployment_page_renders_its_log(self, client, repos):
        response = await client.get("/deployments/blog-3f9a2c71")
        assert response.status_code == 200
        assert "npm run build" in response.text
        assert "healthy after 1.4s" in response.text

    async def test_a_failed_deployment_shows_its_error(self, client, repos):
        response = await client.get("/deployments/blog-9d1e0f44")
        assert "never became healthy" in response.text

    async def test_an_in_flight_deployment_offers_no_promote_button(self, client, repos):
        response = await client.get("/deployments/blog-11c9e2a7")
        assert "Promote to production" not in response.text

    async def test_a_log_line_cannot_inject_markup(self, client, repos, monkeypatch):
        """Build output is attacker-influenced: it contains whatever a
        dependency printed."""
        monkeypatch.setattr(
            "forge.web.routes.deployment_repo.read_logs",
            lambda _id, limit=0, after=0: _async(
                [fakes.log_line(1, "<img src=x onerror=alert(1)>")]
            ),
        )
        response = await client.get("/deployments/blog-3f9a2c71")
        assert "<img src=x" not in response.text
        assert "&lt;img src=x" in response.text

    async def test_a_flash_message_cannot_inject_markup(self, client, repos):
        response = await client.get("/projects/blog?err=<script>alert(1)</script>")
        assert "<script>alert(1)</script>" not in response.text


class TestProjectSummary:
    async def test_a_domain_is_not_shown_before_anything_serves_it(
        self, client, repos, monkeypatch
    ):
        """A project can have a verified domain and no production deployment.
        Listing the domain then points at a hostname that answers with the
        router's default page."""
        undeployed = fakes.project(
            slug="api", name="Orders API", production_deployment_id=None
        )
        monkeypatch.setattr(
            "forge.web.routes.project_repo.list_all", lambda: _async([undeployed])
        )
        response = await client.get("/")
        assert "not deployed yet" in response.text
        assert "example.com" not in response.text


class TestProcesses:
    async def test_the_project_page_lists_workers_and_jobs(self, client, repos):
        response = await client.get("/projects/blog")
        assert "mailer" in response.text
        assert "nightly" in response.text
        assert "0 3 * * *" in response.text or "03:00" in response.text

    async def test_a_cron_page_shows_its_schedule_in_words_and_its_runs(
        self, client, repos
    ):
        response = await client.get("/projects/blog/processes/nightly")
        assert response.status_code == 200
        assert "every day at 03:00 UTC" in response.text
        assert "Run now" in response.text

    async def test_a_worker_page_offers_no_run_now_button(self, client, repos):
        """There is nothing to trigger: it is already running."""
        response = await client.get("/projects/blog/processes/mailer")
        assert response.status_code == 200
        assert "Run now" not in response.text

    async def test_job_output_cannot_inject_markup(self, client, repos, monkeypatch):
        """A job's output is whatever the customer's own code printed."""
        monkeypatch.setattr(
            "forge.web.routes.process_repo.list_runs",
            lambda _id, limit=25: _async(
                [fakes.job_run(output="<img src=x onerror=alert(1)>")]
            ),
        )
        response = await client.get("/projects/blog/processes/nightly")
        assert "<img src=x" not in response.text
        assert "&lt;img src=x" in response.text

    async def test_a_worker_page_says_where_its_output_goes(self, client, repos):
        """Workers stream to the container log rather than the database —
        an always-on process would otherwise write an unbounded log table."""
        response = await client.get("/projects/blog/processes/mailer")
        assert "docker logs" in response.text
