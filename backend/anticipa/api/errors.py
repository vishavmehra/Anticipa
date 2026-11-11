"""Exception handlers producing the contract §2 error envelope. No stack traces, secrets or echoed input."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from anticipa.api.envelope import current_request_id
from anticipa.core.errors import ApiError

log = logging.getLogger(__name__)

_HTTP_CODES = {
    400: "VALIDATION_ERROR", 401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
    405: "VALIDATION_ERROR", 409: "CONFLICT", 413: "VALIDATION_ERROR", 415: "VALIDATION_ERROR",
    422: "VALIDATION_ERROR", 429: "RATE_LIMITED", 503: "PROVIDER_UNAVAILABLE",
}  # fmt: skip
_HTTP_MESSAGES = {
    401: "Authentication is required.", 403: "You do not have permission to perform this action.",
    404: "The requested resource was not found.", 405: "Method not allowed for this resource.",
    429: "Too many requests. Try again later.",
}  # fmt: skip


def error_response(
    request: Request | None,
    status_code: int,
    code: str,
    message: str,
    details: list[dict] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": details or [],
            "request_id": current_request_id(request),
        }
    }
    return JSONResponse(body, status_code=status_code, headers=headers)


def _field_path(loc: tuple) -> str:
    return ".".join(str(p) for p in loc)


async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(request, exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Deliberately omit `input`/`ctx`: request bodies may contain passwords or clinical text.
    details = [{"field": _field_path(e["loc"]), "reason": str(e["msg"])} for e in exc.errors()]
    return error_response(request, 422, "VALIDATION_ERROR", "The supplied request is invalid.", details)


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_CODES.get(
        exc.status_code, "INTERNAL_ERROR" if exc.status_code >= 500 else "VALIDATION_ERROR"
    )
    message = _HTTP_MESSAGES.get(exc.status_code) or (
        "The request could not be processed." if exc.status_code < 500 else "An internal error occurred."
    )
    headers = {"Allow": exc.headers["Allow"]} if exc.headers and "Allow" in exc.headers else None
    return error_response(request, exc.status_code, code, message, headers=headers)


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_error)  # type: ignore[arg-type]
    # Unhandled exceptions are caught by RequestContextMiddleware (it owns the request id and the access log).
