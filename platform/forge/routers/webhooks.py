"""Inbound git webhooks.

Not behind the API token. The endpoint is authenticated by an HMAC over the
request body using a secret held per project, which is what GitHub can
actually produce — and being per project means a compromised or rotated hook
on one repository says nothing about any other.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, Request, status

from forge.deps import SettingsDep
from forge.domain.errors import InvalidRequest, NotFound, Unauthorized
from forge.domain.models import DeploymentTrigger, Project
from forge.engine import service
from forge.repositories import projects as project_repo
from forge.routers.deployments import _out
from forge.routers.schemas import WebhookAccepted

logger = logging.getLogger("forge.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: A body larger than this is not a push event. Read before parsing so that an
#: endpoint reachable from the internet cannot be made to buffer a gigabyte.
MAX_BODY_BYTES = 1_000_000


@router.post("/{project_slug}", status_code=status.HTTP_202_ACCEPTED)
async def github_push(
    project_slug: str,
    request: Request,
    settings: SettingsDep,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> WebhookAccepted:
    project = await project_repo.get_by_slug(project_slug)

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise InvalidRequest("Webhook payload is too large")

    _verify_signature(body, x_hub_signature_256, project.webhook_secret)

    if x_github_event == "ping":
        return WebhookAccepted(ignored="ping — the hook is wired up correctly")
    if x_github_event != "push":
        return WebhookAccepted(ignored=f"{x_github_event} events are not deployed")

    payload = await request.json()
    return await _handle_push(project, payload, settings)


async def _handle_push(project: Project, payload: dict, settings) -> WebhookAccepted:
    ref = payload.get("ref") or ""
    if not ref.startswith("refs/heads/"):
        return WebhookAccepted(ignored=f"{ref} is not a branch")
    branch = ref[len("refs/heads/") :]

    if payload.get("deleted"):
        return WebhookAccepted(ignored=f"{branch} was deleted")

    sha = payload.get("after") or ""
    # GitHub sends this all-zero sha for a deleted ref; the `deleted` flag
    # above normally catches it first, but not every git host sets that flag.
    if not sha or set(sha) == {"0"}:
        return WebhookAccepted(ignored=f"{branch} has no commit to build")

    head = payload.get("head_commit") or {}
    author = (head.get("author") or {}).get("name")

    deployment = await service.queue_deploy(
        project,
        ref=branch,
        sha=sha,
        trigger=DeploymentTrigger.PUSH,
        message=head.get("message", "").split("\n")[0] or None,
        author=author,
    )
    logger.info(
        "queued %s #%s from a push to %s", deployment.short_id, deployment.number, branch
    )
    return WebhookAccepted(deployment=_out(deployment, project, settings))


def _verify_signature(body: bytes, header: str | None, secret: str) -> None:
    """Reject anything not signed with this project's secret.

    Compared with `compare_digest` for the same reason the API token is: a
    byte-by-byte comparison leaks how much of a forged signature was correct,
    and a webhook endpoint is reachable by anyone who can guess the project
    slug.
    """
    if not header:
        raise Unauthorized(
            "This webhook is not signed. Set the secret in the repository's "
            "webhook settings."
        )
    if not header.startswith("sha256="):
        raise Unauthorized("Unsupported webhook signature algorithm")

    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(header[len("sha256=") :], expected):
        raise Unauthorized("The webhook signature does not match")


@router.get("/{project_slug}/secret")
async def reveal_secret(project_slug: str) -> dict[str, str]:
    """Deliberately absent. Kept as a route so the 404 explains itself."""
    raise NotFound(
        "Webhook secrets are not readable through the API. Run "
        f"`forge webhook {project_slug}` on the host to print it."
    )
