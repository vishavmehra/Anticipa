"""/health/ready degradation and /integrations (Claude + Nebius via ai.health, cached 5 min). No database."""

from __future__ import annotations

import time

import pytest

from anticipa.ai import health as ai_health
from anticipa.api import integrations


def test_ready_reports_unreachable_database_as_503(api_client_nodb):
    resp = api_client_nodb.get("/api/v1/health/ready")
    assert resp.status_code == 503
    data = resp.json()["data"]
    assert (
        data["status"] == "not_ready"
        and data["database"] == "unreachable"
        and data["migrations"] == "unknown"
    )


@pytest.fixture
def provider_stub(monkeypatch):
    """Fake models-list results; postgres/hapi checks are stubbed (no DB, no HTTP)."""
    calls = {"n": 0}
    result = {
        "anthropic": ai_health.ProviderHealth("anthropic", "ok", 120, 9),
        "nebius": ai_health.ProviderHealth("nebius", "not_configured", detail="NEBIUS_API_KEY is not set"),
    }

    async def fake_check_providers(*_a, **_k):
        calls["n"] += 1
        return result

    monkeypatch.setattr(ai_health, "check_providers", fake_check_providers)
    for key in ("postgres", "hapi"):
        monkeypatch.setitem(
            integrations._REGISTRY,
            key,
            integrations.Integration(key, key, lambda *_: (True, "healthy", None)),
        )
    monkeypatch.setattr(integrations, "_provider_cache", None)
    return calls, result


def test_integrations_include_claude_and_nebius(api_client_nodb, auth_headers, provider_stub):
    calls, _ = provider_stub
    resp = api_client_nodb.get("/api/v1/integrations", headers=auth_headers("observer"))
    assert resp.status_code == 200
    by_key = {i["key"]: i for i in resp.json()["data"]}
    assert {"postgres", "hapi", "claude", "nebius"} <= set(by_key)
    assert by_key["claude"]["status"] == "healthy" and by_key["claude"]["configured"] is True
    assert "9 models" in by_key["claude"]["detail"]
    assert by_key["nebius"]["status"] == "not_configured" and by_key["nebius"]["configured"] is False
    assert calls["n"] == 1  # one probe serves both integrations


def test_provider_probe_is_cached_for_five_minutes(api_client_nodb, auth_headers, provider_stub):
    calls, _ = provider_stub
    for _ in range(3):
        assert (
            api_client_nodb.get("/api/v1/integrations", headers=auth_headers("observer")).status_code == 200
        )
    assert (
        api_client_nodb.get(
            "/api/v1/integrations/claude/health", headers=auth_headers("observer")
        ).status_code
        == 200
    )
    assert calls["n"] == 1
    cached = integrations._provider_cache
    integrations._provider_cache = (time.monotonic() - integrations.PROVIDER_CACHE_TTL_SECONDS - 1, cached[1])
    api_client_nodb.get("/api/v1/integrations", headers=auth_headers("observer"))
    assert calls["n"] == 2


@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [("auth", "authentication_failed"), ("timeout", "unavailable"), ("rate_limited", "degraded"),
     ("degraded", "degraded"), ("weird_new_category", "unavailable")],
)  # fmt: skip
def test_provider_status_mapping(api_client_nodb, auth_headers, provider_stub, provider_status, expected):
    _, result = provider_stub
    result["anthropic"] = ai_health.ProviderHealth("anthropic", provider_status, detail="x" * 500)
    resp = api_client_nodb.get("/api/v1/integrations/claude/health", headers=auth_headers("system_admin"))
    data = resp.json()["data"]
    assert data["status"] == expected and len(data["detail"]) <= 300


def test_unknown_integration_is_404_and_role_less_is_403(
    api_client_nodb, auth_headers, mint_token, provider_stub
):
    assert (
        api_client_nodb.get("/api/v1/integrations/nope/health", headers=auth_headers("observer")).status_code
        == 404
    )
    headers = {"Authorization": f"Bearer {mint_token(None)}"}
    assert api_client_nodb.get("/api/v1/integrations", headers=headers).status_code == 403
