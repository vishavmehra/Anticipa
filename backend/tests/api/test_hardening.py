"""API hardening (security gate 2026-11-13): security headers, request body cap, no CORS grants.
No database needed: all of this happens before any route body."""

from __future__ import annotations

from anticipa.api.middleware import MAX_BODY_BYTES

API = "/api/v1"


def test_security_headers_and_no_store_by_default(api_client_nodb):
    resp = api_client_nodb.get(f"{API}/health")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cross-origin-resource-policy"] == "same-origin"
    assert "max-age=" in resp.headers["strict-transport-security"]
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["cache-control"] == "no-store"


def test_error_responses_carry_the_headers_too(api_client_nodb):
    resp = api_client_nodb.get(f"{API}/jobs")  # 401
    assert resp.status_code == 401
    assert resp.headers["x-content-type-options"] == "nosniff" and resp.headers["cache-control"] == "no-store"


def test_swagger_docs_get_no_csp_so_the_ui_still_loads(api_client_nodb):
    resp = api_client_nodb.get(f"{API}/docs")
    assert resp.status_code == 200
    assert (
        "content-security-policy" not in resp.headers and resp.headers["x-content-type-options"] == "nosniff"
    )


def test_declared_oversized_body_is_rejected_with_413_envelope(api_client_nodb, auth_headers):
    resp = api_client_nodb.post(
        f"{API}/ask", headers=auth_headers("observer"), content=b"x" * (MAX_BODY_BYTES + 1)
    )
    assert resp.status_code == 413
    error = resp.json()["error"]
    assert (
        error["code"] == "VALIDATION_ERROR"
        and error["request_id"]
        and resp.headers["x-request-id"] == error["request_id"]
    )


def test_chunked_oversized_body_is_rejected_with_413(api_client_nodb, auth_headers):
    def chunks():
        for _ in range(MAX_BODY_BYTES // 65536 + 2):
            yield b"x" * 65536

    resp = api_client_nodb.post(f"{API}/ask", headers=auth_headers("observer"), content=chunks())
    assert resp.status_code == 413


def test_a_normal_sized_body_is_not_blocked(api_client_nodb, auth_headers):
    resp = api_client_nodb.post(f"{API}/ask", headers=auth_headers("observer"), json={})
    assert resp.status_code == 422  # validation, not 413


def test_no_cors_grant_for_a_foreign_origin(api_client_nodb):
    resp = api_client_nodb.get(f"{API}/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers
    pre = api_client_nodb.options(
        f"{API}/health", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
    )
    assert "access-control-allow-origin" not in pre.headers
