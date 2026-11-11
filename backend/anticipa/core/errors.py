"""Domain errors that map 1:1 onto the contract §2 error envelope (codes + HTTP status)."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"
    default_message = "An internal error occurred."
    headers: dict[str, str] | None = None

    def __init__(self, message: str | None = None, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        self.details = details or []


class ValidationFailed(ApiError):
    status_code, code, default_message = 422, "VALIDATION_ERROR", "The supplied request is invalid."


class Unauthenticated(ApiError):
    status_code, code, default_message = 401, "UNAUTHENTICATED", "Authentication is required."
    headers = {"WWW-Authenticate": "Bearer"}


class Forbidden(ApiError):
    status_code, code, default_message = (
        403,
        "FORBIDDEN",
        "You do not have permission to perform this action.",
    )


class NotFound(ApiError):
    status_code, code, default_message = 404, "NOT_FOUND", "The requested resource was not found."


class Conflict(ApiError):
    status_code, code, default_message = 409, "CONFLICT", "The request conflicts with the current state."


class InvalidTransition(ApiError):
    status_code, code, default_message = (
        409,
        "INVALID_TRANSITION",
        "The requested state transition is not allowed.",
    )


class RateLimited(ApiError):
    status_code, code, default_message = 429, "RATE_LIMITED", "Too many requests. Try again later."


class ProviderUnavailable(ApiError):
    status_code, code, default_message = 503, "PROVIDER_UNAVAILABLE", "An upstream provider is unavailable."
