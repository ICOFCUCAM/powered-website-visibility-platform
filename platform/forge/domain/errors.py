"""The errors the platform raises on purpose.

Every one carries an HTTP status because the API is the only caller that has
to turn them into a response, and a mapping table kept somewhere else drifts.
"""

from __future__ import annotations


class ForgeError(Exception):
    """Base class. Never raised directly."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(ForgeError):
    status_code = 404
    code = "not_found"


class Conflict(ForgeError):
    """The request is valid but the current state forbids it.

    A rollback to a deployment that never became ready, a promotion of a
    deployment belonging to a different project, a second deployment of a
    commit already building.
    """

    status_code = 409
    code = "conflict"


class InvalidRequest(ForgeError):
    status_code = 400
    code = "invalid_request"


class Unauthorized(ForgeError):
    status_code = 401
    code = "unauthorized"


class DetectionFailed(ForgeError):
    """No build strategy fits the repository.

    Carries the paths that were looked for, because the only useful version of
    this error tells the owner what to add.
    """

    status_code = 422
    code = "detection_failed"


class BuildFailed(ForgeError):
    """The image did not build. The reason is in the deployment's logs."""

    status_code = 422
    code = "build_failed"


class DeployFailed(ForgeError):
    """The image built but the container never became healthy."""

    status_code = 422
    code = "deploy_failed"
