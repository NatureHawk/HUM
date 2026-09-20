"""Domain errors.

Services raise these; a single exception handler in `app.main` turns them into
HTTP responses. Routes never build error responses themselves, so an error code
means the same thing no matter which endpoint produced it.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for every expected (non-bug) failure."""

    status_code: int = 400
    code: str = "domain_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class NoteGoneError(DomainError):
    """The note was deleted by someone else while this client was working on it."""

    status_code = 404
    code = "note_gone"


class StrokeGoneError(DomainError):
    """The stroke was deleted by someone else while this client was working on it."""

    status_code = 404
    code = "stroke_gone"


class UnauthorizedError(DomainError):
    """No valid guest session on the request."""

    status_code = 401
    code = "no_session"


class BoardFullError(DomainError):
    """The board has hit the public-demo note ceiling."""

    status_code = 409
    code = "board_full"


class VersionConflictError(DomainError):
    """Optimistic concurrency check failed - somebody committed first.

    `details["note"]` carries the current canonical note so the client can show
    a real conflict instead of guessing.
    """

    status_code = 409
    code = "version_conflict"


class AnchorMissingError(DomainError):
    """A move referenced a neighbour card that is no longer where the client saw it."""

    status_code = 409
    code = "anchor_missing"


class InvalidOperationError(DomainError):
    status_code = 422
    code = "invalid_operation"
