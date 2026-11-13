"""ADR-060: the observer role cannot change anything. Every mutating route that exists is enumerated from the
app
itself, so routes added by other modules are covered automatically. No database needed: permission checks run
before any route body."""

from __future__ import annotations

import re

import pytest

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
PARAM = re.compile(r"\{[^}]+\}")
DUMMY_ID = "00000000-0000-4000-8000-000000000001"
# POST endpoints that only compute and persist nothing (guarded by a *.view permission, so the observer may
# call
# them). Adding to this list needs a reviewer's justification.
# POST /ask is a natural language read: it runs read-only aggregate tools and one audit event
# (ask.query), changes no product data, and is guarded by analytics.view (ADR-069); a positive
# observer test lives in tests/ask/test_tools_api.py.
READ_ONLY_POSTS = {("POST", "/api/v1/rules/validate"), ("POST", "/api/v1/ask")}


def mutating_routes(app):
    """(method, path) for every non-GET operation in the OpenAPI schema (FastAPI mounts routers lazily)."""
    routes = []
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            if method.upper() in MUTATING:
                routes.append((method.upper(), PARAM.sub(DUMMY_ID, path)))
    return sorted(routes)


def test_mutating_routes_exist(api_client_nodb):
    found = mutating_routes(api_client_nodb.app)
    assert ("POST", f"/api/v1/jobs/{DUMMY_ID}/retry") in found
    assert ("POST", f"/api/v1/jobs/{DUMMY_ID}/cancel") in found


def test_observer_gets_403_on_every_mutating_route(api_client_nodb, auth_headers):
    routes = mutating_routes(api_client_nodb.app)
    assert routes
    wrong = {}
    for method, path in routes:
        if (method, path) in READ_ONLY_POSTS:
            continue
        resp = api_client_nodb.request(method, path, headers=auth_headers("observer"), json={})
        if resp.status_code != 403 or resp.json()["error"]["code"] != "FORBIDDEN":
            wrong[f"{method} {path}"] = resp.status_code
    assert not wrong, wrong


def test_unauthenticated_gets_401_on_every_mutating_route(api_client_nodb):
    wrong = {}
    for method, path in mutating_routes(api_client_nodb.app):
        resp = api_client_nodb.request(method, path, json={})
        if resp.status_code != 401:
            wrong[f"{method} {path}"] = resp.status_code
    assert not wrong, wrong


def test_role_less_user_gets_403_everywhere(api_client_nodb, mint_token):
    headers = {"Authorization": f"Bearer {mint_token(None)}"}
    for method, path in mutating_routes(api_client_nodb.app):
        assert api_client_nodb.request(method, path, headers=headers, json={}).status_code == 403


@pytest.mark.parametrize("path", ["/api/v1/jobs", "/api/v1/audit", "/api/v1/integrations"])
def test_view_routes_reject_unauthenticated(api_client_nodb, path):
    assert api_client_nodb.get(path).status_code == 401


def test_liveness_is_public(api_client_nodb):
    resp = api_client_nodb.get("/api/v1/health")
    assert resp.status_code == 200 and resp.json()["data"] == {"status": "ok"}
