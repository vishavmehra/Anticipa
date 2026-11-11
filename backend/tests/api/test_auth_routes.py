"""GET /api/v1/auth/me and bearer-only authentication (ADR-057); no database."""

from __future__ import annotations

from anticipa.core.rbac import permissions_for

from ..conftest import role_user_id  # noqa: TID252


def test_me_returns_id_email_role_permissions(api_client_nodb, auth_headers):
    resp = api_client_nodb.get("/api/v1/auth/me", headers=auth_headers("observer"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["request_id"] and resp.headers["x-request-id"] == body["meta"]["request_id"]
    data = body["data"]
    assert data["id"] == str(role_user_id("observer"))
    assert data["email"] == "observer@example.test"
    assert data["role"] == "observer"
    assert data["permissions"] == sorted(permissions_for("observer"))
    assert set(data) == {"id", "email", "display_name", "role", "permissions"}


def test_missing_token_is_401_with_bearer_challenge(api_client_nodb):
    resp = api_client_nodb.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


def test_non_bearer_scheme_is_401(api_client_nodb):
    assert api_client_nodb.get("/api/v1/auth/me", headers={"Authorization": "Basic YTpi"}).status_code == 401


def test_tokens_in_cookies_or_query_strings_are_ignored(api_client_nodb, mint_token):
    token = mint_token("system_admin")
    for cookie in ("access_token", "sb-test-project-auth-token", "token"):
        api_client_nodb.cookies.set(cookie, token)
    assert api_client_nodb.get("/api/v1/auth/me").status_code == 401
    assert (
        api_client_nodb.get("/api/v1/auth/me", params={"access_token": token, "token": token}).status_code
        == 401
    )


def test_invalid_token_is_401_and_not_echoed(api_client_nodb):
    resp = api_client_nodb.get("/api/v1/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401
    assert "not.a.jwt" not in resp.text


def test_user_without_a_role_is_403(api_client_nodb, mint_token):
    resp = api_client_nodb.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {mint_token(None)}"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_role_only_in_user_metadata_is_403(api_client_nodb, mint_token):
    token = mint_token(None, app_metadata={}, user_metadata={"anticipa_role": "system_admin"})
    assert (
        api_client_nodb.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 403
    )


def test_expired_token_is_401(api_client_nodb, mint_token):
    token = mint_token("observer", expires_in=-600)
    assert (
        api_client_nodb.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_unreachable_jwks_is_503_envelope(api_client_nodb, jwks, mint_token):
    token = mint_token("observer")
    jwks.unreachable = True
    resp = api_client_nodb.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"


def test_no_password_cookie_or_csrf_surface_remains(api_client_nodb):
    paths = set(api_client_nodb.get("/api/v1/openapi.json").json()["paths"])
    assert not {p for p in paths if any(w in p for w in ("login", "logout", "csrf", "password", "session"))}
    schemes = api_client_nodb.get("/api/v1/openapi.json").json()["components"]["securitySchemes"]
    assert all(s["type"] == "http" and s["scheme"] == "bearer" for s in schemes.values())
