"""Request dependencies.

One user, one token. There is no session, no login page and no user table,
because the audience decision was made at the start: this platform hosts its
owner's sites. Adding accounts later means adding a table and changing this
module, and nothing else — every route already asks for "the caller", not
"the token".
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, Request

from forge.config import Settings, get_settings
from forge.domain import session
from forge.domain.errors import NotFound, Unauthorized
from forge.domain.models import Project
from forge.repositories import projects as project_repo


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


def token_matches(presented: str, expected: str) -> bool:
    """`hmac.compare_digest` rather than `==`.

    A plain comparison returns as soon as two bytes differ, so the time it
    takes measures how many leading characters were right. It is a slow attack
    over a network and a fast one from the same host, and the fix costs
    nothing.
    """
    return hmac.compare_digest(presented, expected)


async def require_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    settings: SettingsDep = None,  # type: ignore[assignment]
) -> None:
    """Accept a bearer token, or the dashboard's session cookie.

    Two credentials, one check. A browser cannot attach an Authorization
    header to a plain navigation or to an EventSource, so the dashboard signs
    in once and holds a cookie derived from the same token — rather than the
    platform growing a second, separately-revocable secret.
    """
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization.split(" ", 1)[1].strip()
        if token_matches(presented, settings.api_token):
            return
        raise Unauthorized("That token is not valid")

    cookie = request.cookies.get(session.COOKIE_NAME)
    if cookie:
        try:
            session.verify(cookie, token=settings.api_token)
        except session.InvalidSession as exc:
            raise Unauthorized(f"Your session is not valid: {exc}") from exc
        return

    raise Unauthorized("Provide a bearer token")


Authenticated = Depends(require_token)


async def get_project(project_ref: str) -> Project:
    """Resolve the `{project_ref}` path parameter, which may be a slug or id."""
    try:
        return await project_repo.resolve(project_ref)
    except NotFound:
        raise NotFound(f"No project {project_ref!r}") from None


ProjectDep = Annotated[Project, Depends(get_project)]
