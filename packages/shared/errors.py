"""Application errors and RFC 7807 problem-detail responses.

ARCHITECTURE.md §5.7 specifies problem details for every error response.
Stack traces are never returned to the client (SECURITY.md §8.1).
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for errors that map cleanly to an HTTP response."""

    status_code: int = 500
    title: str = "Internal Server Error"
    error_type: str = "about:blank"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)

    def to_problem(self, instance: str | None = None) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": self.error_type,
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
        }
        if instance:
            problem["instance"] = instance
        problem.update(self.extra)
        return problem


class NotFoundError(AppError):
    status_code = 404
    title = "Not Found"
    error_type = "https://jobforge.local/errors/not-found"


class ValidationError(AppError):
    status_code = 422
    title = "Validation Failed"
    error_type = "https://jobforge.local/errors/validation"


class ConflictError(AppError):
    status_code = 409
    title = "Conflict"
    error_type = "https://jobforge.local/errors/conflict"


class ForbiddenError(AppError):
    status_code = 403
    title = "Forbidden"
    error_type = "https://jobforge.local/errors/forbidden"


class DependencyUnavailableError(AppError):
    status_code = 503
    title = "Dependency Unavailable"
    error_type = "https://jobforge.local/errors/dependency-unavailable"


class PolicyBlockedError(AppError):
    """Raised when site_policies forbids an action (SECURITY.md §7.2)."""

    status_code = 403
    title = "Blocked by Site Policy"
    error_type = "https://jobforge.local/errors/policy-blocked"
