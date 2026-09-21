"""Application errors with the stable codes the UI branches on.

Messages are written for a non-technical reader and shipped to the user
verbatim, per the V1 spec (s38). A code is a contract: renaming one is a
breaking API change.
"""

from __future__ import annotations


class AppError(Exception):
    """Base for every error the API turns into a structured response."""

    code = "internal_error"
    status = 500
    message = "Something went wrong. Please try again."
    retriable = True

    def __init__(self, message: str | None = None, **details: object) -> None:
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)

    def as_payload(self) -> dict[str, object]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "retriable": self.retriable,
            }
        }


class NotAuthenticated(AppError):
    code = "not_authenticated"
    status = 401
    message = "Please sign in to continue."
    retriable = False


class Forbidden(AppError):
    code = "forbidden"
    status = 403
    message = "You don't have access to this."
    retriable = False


class NotFound(AppError):
    code = "not_found"
    status = 404
    message = "We couldn't find that."
    retriable = False


class InvalidWebsite(AppError):
    code = "invalid_website"
    status = 422
    message = "That doesn't look like a website address."
    retriable = False


class WebsiteAlreadyExists(AppError):
    code = "website_already_exists"
    status = 409
    message = "You've already added this website."
    retriable = False


class PlanLimitExceeded(AppError):
    code = "plan_limit_exceeded"
    status = 402
    message = "You've reached your plan's limit."
    retriable = False


class CrawlNotAllowed(AppError):
    """Raised when crawl prerequisites fail (decision 20).

    Evaluated at admission AND again by the crawler before fetching, because a
    queue entry must never outlive the permission that created it.
    """

    code = "crawl_not_allowed"
    status = 403
    message = "We can't crawl this website yet. Verify that you own it first."
    retriable = False

    def __init__(self, message: str | None = None, **details: object) -> None:
        # The reason is machine-readable so the UI can say which prerequisite
        # failed, rather than repeating one generic sentence for all of them.
        reason = details.pop("details_reason", None)
        if reason:
            details["reason"] = reason
        super().__init__(message, **details)


class GoogleAuthFailed(AppError):
    code = "google_auth_failed"
    status = 502
    message = "We couldn't connect your Google account. Please try again."


class GoogleNeedsReauth(AppError):
    code = "google_needs_reauth"
    status = 409
    message = "Your Google connection needs attention."
    retriable = False


class WebsiteUnreachable(AppError):
    code = "website_unreachable"
    status = 422
    message = "We couldn't reach this website. Check that it is publicly accessible."
    retriable = False
