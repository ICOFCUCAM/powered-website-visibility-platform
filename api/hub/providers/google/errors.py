"""Google API failures, translated into things the product can act on."""

from __future__ import annotations

from api.domain.errors import AppError


class GoogleError(AppError):
    code = "google_error"
    status = 502
    message = "Google couldn't complete that request. Please try again."


class GoogleAuthorizationFailed(GoogleError):
    code = "google_auth_failed"
    status = 400
    message = "We couldn't connect your Google account. Please try again."
    retriable = False


class GoogleRefreshRejected(GoogleError):
    """`invalid_grant`: the refresh token is dead.

    Revoked in the Google account, password changed, or unused for six months.
    Never retried — retrying a dead grant is how an OAuth client gets rate
    limited. The connection is marked `needs_reauth` and sync stops.
    """

    code = "google_needs_reauth"
    status = 409
    message = "Your Google connection needs attention."
    retriable = False


class GoogleQuotaExceeded(GoogleError):
    code = "google_quota_exceeded"
    status = 429
    message = "Google is rate-limiting us. We'll retry shortly."
    retriable = True


class GooglePermissionDenied(GoogleError):
    code = "google_permission_denied"
    status = 403
    message = "Your Google account doesn't have access to that."
    retriable = False
