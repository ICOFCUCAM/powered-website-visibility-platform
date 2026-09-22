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

from fastapi import Depends, Header

from forge.config import Settings, get_settings
from forge.domain.errors import NotFound, Unauthorized
from forge.domain.models import Project
from forge.repositories import projects as project_repo


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


async def require_token(
    authorization: Annotated[str | None, Header()] = None,
    settings: SettingsDep = None,  # type: ignore[assignment]
) -> None:
    """Check the bearer token in constant time.

    `hmac.compare_digest` rather than `==`: a plain comparison returns as soon
    as two bytes differ, and the time it takes is a measurement of how many
    leading characters were right. It is a slow attack over a network and a
    fast one from the same host, and the fix costs nothing.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Provide a bearer token")
    presented = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(presented, settings.api_token):
        raise Unauthorized("That token is not valid")


Authenticated = Depends(require_token)


async def get_project(project_ref: str) -> Project:
    """Resolve the `{project_ref}` path parameter, which may be a slug or id."""
    try:
        return await project_repo.resolve(project_ref)
    except NotFound:
        raise NotFound(f"No project {project_ref!r}") from None


ProjectDep = Annotated[Project, Depends(get_project)]
