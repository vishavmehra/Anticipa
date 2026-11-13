"""Pure-ASGI middleware: request id, access log, security headers, request body cap, and the
INTERNAL_ERROR envelope for unhandled exceptions."""

from __future__ import annotations

import logging
import re
import time
import uuid

import orjson
from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from anticipa.api.errors import error_response
from anticipa.core.logging import request_id_var

log = logging.getLogger("anticipa.access")
REQUEST_ID_HEADER = "x-request-id"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# The largest legitimate JSON body (a guideline version with 64 rules) is far below this.
MAX_BODY_BYTES = 2 * 1024 * 1024
_DOCS_PREFIX = "/api/v1/docs"  # Swagger UI loads its script from a CDN, so it gets no CSP
# Security headers on every API response (JSON only). Cache-Control is a default: analytics set their own.
_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
)
_API_CSP = (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")


class _BodyTooLarge(
    HTTPException
):  # an HTTPException so FastAPI's body reader re-raises it instead of answering 400
    def __init__(self) -> None:
        super().__init__(413)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_headers = dict(scope["headers"])
        incoming = request_headers.get(REQUEST_ID_HEADER.encode(), b"").decode("latin-1")
        request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500
        response_started = False
        received = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", request_id.encode()))
                present = {name.lower() for name, _ in headers}
                headers.extend(h for h in _SECURITY_HEADERS if h[0] not in present)
                if not scope["path"].startswith(_DOCS_PREFIX):
                    headers.append(_API_CSP)
                if b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
            await send(message)

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:  # chunked bodies carry no Content-Length to check up front
                    raise _BodyTooLarge()
            return message

        try:
            declared = request_headers.get(b"content-length", b"")
            if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                raise _BodyTooLarge()
            await self.app(scope, limited_receive, send_wrapper)
        except _BodyTooLarge as exc:  # raised here (Content-Length) or while the route reads a chunked body
            if response_started:
                raise
            response = error_response(
                None, exc.status_code, "VALIDATION_ERROR", "The request body is too large."
            )
            await response(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled exception", extra={"method": scope["method"], "path": scope["path"]})
            if response_started:  # cannot change a response already in flight
                raise
            body = orjson.dumps(
                {
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "An internal error occurred.",
                        "details": [],
                        "request_id": request_id,
                    }
                }
            )
            await send_wrapper(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send_wrapper({"type": "http.response.body", "body": body})
        finally:
            log.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],  # path only: query strings are never logged
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            request_id_var.reset(token)
