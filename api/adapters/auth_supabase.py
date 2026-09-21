"""Identity provider adapter.

The only thing the rest of the application asks of auth is: "verify this token,
give me a user id". Supabase Auth answers it today; swapping providers means
replacing this file and nothing else.

Credentials are never stored by this application (see migration 0001): there is
no `password_hash` column, because a table that cannot leak one is strictly
safer than a table that can.
"""

from __future__ import annotations

from uuid import UUID

import jwt

from api.config import Settings
from api.domain.errors import NotAuthenticated
from api.domain.models import Principal


def principal_from_token(token: str, settings: Settings) -> Principal:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            options={
                "require": ["sub", "exp"],
                "verify_aud": settings.jwt_audience is not None,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise NotAuthenticated("Your session has expired. Please sign in again.") from exc
    except jwt.InvalidTokenError as exc:
        raise NotAuthenticated() from exc

    try:
        user_id = UUID(str(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise NotAuthenticated() from exc

    email = claims.get("email") or ""
    return Principal(user_id=user_id, email=email)
