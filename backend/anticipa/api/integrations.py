"""Integration health (05 §26). Lightweight connectivity/metadata checks only: no credentials exposed, no
model
calls, no clinical work. Other workstreams extend this with `register_integration(...)`."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from anticipa.ai import health as ai_health
from anticipa.api.deps import SettingsDep
from anticipa.api.envelope import Envelope, ok
from anticipa.config import Settings
from anticipa.core.auth import require_permission
from anticipa.core.errors import NotFound
from anticipa.core.rbac import INTEGRATIONS_VIEW

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/integrations",
    tags=["integrations"],
    dependencies=[Depends(require_permission(INTEGRATIONS_VIEW))],
)

Status = Literal["healthy", "degraded", "unavailable", "not_configured", "authentication_failed", "unknown"]


class IntegrationHealth(BaseModel):
    key: str
    name: str
    configured: bool
    status: Status
    detail: str | None = None
    latency_ms: float | None = None
    checked_at: datetime


@dataclass(frozen=True)
class Integration:
    key: str
    name: str
    check: Callable[[Request, Settings], tuple[bool, Status, str | None]]  # (configured, status, detail)


_REGISTRY: dict[str, Integration] = {}


def register_integration(
    key: str, name: str, check: Callable[[Request, Settings], tuple[bool, Status, str | None]]
) -> None:
    """Add or replace an integration check (e.g. the AI workstream swaps in real Claude/Nebius checks)."""
    _REGISTRY[key] = Integration(key, name, check)


def _postgres(request: Request, _settings: Settings) -> tuple[bool, Status, str | None]:
    try:
        session: Session
        with request.app.state.sessionmaker() as session:
            session.execute(text("SELECT 1"))
            row = session.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).first()
    except Exception:
        log.warning("postgres health check failed", exc_info=True)
        return True, "unavailable", "Database is unreachable."
    if row is None:
        return True, "degraded", "Connected, but the pgvector extension is not installed."
    return True, "healthy", f"Connected; pgvector {row[0]}."


def _hapi(_request: Request, settings: Settings) -> tuple[bool, Status, str | None]:
    url = (
        settings.hapi_fhir_base_url.rstrip("/").replace("//localhost", "//127.0.0.1") + "/metadata"
    )  # see config.prefer_ipv4_loopback
    try:
        resp = httpx.get(
            url,
            params={"_summary": "true"},
            headers={"Accept": "application/fhir+json"},
            timeout=settings.integration_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        return True, "unavailable", f"HAPI FHIR is unreachable ({type(exc).__name__})."
    if resp.status_code in (401, 403):
        return True, "authentication_failed", f"HAPI FHIR rejected the request (HTTP {resp.status_code})."
    if resp.status_code != 200:
        return True, "unavailable", f"HAPI FHIR returned HTTP {resp.status_code}."
    try:
        version = str(resp.json().get("fhirVersion", ""))
    except ValueError:
        return True, "degraded", "HAPI FHIR returned a non-JSON capability statement."
    if not version.startswith("4.0"):
        return True, "degraded", f"Unexpected FHIR version {version or 'unknown'} (expected R4 4.0.x)."
    return True, "healthy", f"FHIR R4 server (fhirVersion {version})."


PROVIDER_CACHE_TTL_SECONDS = 300.0  # models-list calls are free, but never run them per request
_STATUS_FROM_PROVIDER: dict[str, Status] = {
    "ok": "healthy",
    "degraded": "degraded",
    "not_configured": "not_configured",
    "auth": "authentication_failed",
    "rate_limited": "degraded",
    "timeout": "unavailable",
    "unavailable": "unavailable",
}  # fmt: skip  (anything else -> "unavailable")
_provider_lock = threading.Lock()
_provider_cache: tuple[float, dict] | None = None  # (monotonic timestamp, {provider: ProviderHealth})


def _provider_health() -> dict:
    """`anticipa.ai.health.check_providers()` (models-list only), cached for 5 minutes and shared by both
    checks."""
    global _provider_cache
    with _provider_lock:
        if _provider_cache is not None and time.monotonic() - _provider_cache[0] < PROVIDER_CACHE_TTL_SECONDS:
            return _provider_cache[1]
        result = asyncio.run(
            ai_health.check_providers()
        )  # sync endpoints run in a worker thread: no running loop
        _provider_cache = (time.monotonic(), result)
        return result


def _provider(provider: str) -> Callable[[Request, Settings], tuple[bool, Status, str | None]]:
    def check(_request: Request, _settings: Settings) -> tuple[bool, Status, str | None]:
        health = _provider_health()[provider]
        status: Status = _STATUS_FROM_PROVIDER.get(health.status, "unavailable")
        detail = health.detail
        if status == "healthy":
            detail = f"{health.model_count} models listed ({health.latency_ms} ms)."
        return status != "not_configured", status, detail[:300] if detail else None

    return check


register_integration("postgres", "PostgreSQL + pgvector", _postgres)
register_integration("hapi", "HAPI FHIR R4", _hapi)
register_integration("claude", "Anthropic Claude", _provider("anthropic"))
register_integration("nebius", "Nebius Token Factory", _provider("nebius"))


def _run(integration: Integration, request: Request, settings: Settings) -> IntegrationHealth:
    started = time.perf_counter()
    try:
        configured, status, detail = integration.check(request, settings)
    except Exception:
        log.exception("integration check crashed", extra={"integration": integration.key})
        configured, status, detail = True, "unknown", "Health check failed unexpectedly."
    return IntegrationHealth(
        key=integration.key,
        name=integration.name,
        configured=configured,
        status=status,
        detail=detail,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        checked_at=datetime.now(UTC),
    )


@router.get(
    "", response_model=Envelope[list[IntegrationHealth]], summary="Configured integrations and their health"
)
def list_integrations(request: Request, settings: SettingsDep):
    return ok(request, [_run(i, request, settings) for i in _REGISTRY.values()])


@router.get(
    "/{integration_key}/health",
    response_model=Envelope[IntegrationHealth],
    summary="Health of one integration",
)
def integration_health(integration_key: str, request: Request, settings: SettingsDep):
    integration = _REGISTRY.get(integration_key)
    if integration is None:
        raise NotFound("Unknown integration.")
    return ok(request, _run(integration, request, settings))
