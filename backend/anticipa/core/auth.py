"""Supabase Auth bearer-token verification, current user, and permission dependencies (ADR-058).

Only `Authorization: Bearer <jwt>` is accepted (never cookies or query parameters). Tokens are verified
against the
project's JWKS (ES256/RS256), audience `authenticated`, issuer `<SUPABASE_URL>/auth/v1`, with `exp` and `sub`
required.
The Anticipa role comes ONLY from `app_metadata.anticipa_role` (writable by the service key alone), never from
`user_metadata` (writable by the user). Note: a role change takes effect when the user's access token is
refreshed.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from anticipa.config import Settings
from anticipa.core.audit import Actor
from anticipa.core.errors import Forbidden, ProviderUnavailable, Unauthenticated
from anticipa.core.models import UserProfile
from anticipa.core.rbac import ROLES, has_permission, permissions_for

log = logging.getLogger(__name__)

ALGORITHMS = ["ES256", "RS256"]
AUDIENCE = "authenticated"
ROLE_CLAIM = "anticipa_role"
LEEWAY_SECONDS = 10

_bearer = HTTPBearer(auto_error=False, description="Supabase Auth access token")


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    email: str | None
    role: str
    display_name: str | None = None

    @property
    def permissions(self) -> frozenset[str]:
        return permissions_for(self.role)

    def can(self, permission: str) -> bool:
        return has_permission(self.role, permission)

    @property
    def actor(self) -> Actor:
        return Actor(self.id, self.role)


@lru_cache(maxsize=4)
def get_jwks_client(jwks_url: str) -> PyJWKClient:
    """One cached client per JWKS URL (PyJWKClient caches the key set, refreshing on an unknown `kid`)."""
    return PyJWKClient(jwks_url, cache_keys=True, cache_jwk_set=True, lifespan=600, timeout=5)


def _display_name(claims: dict[str, Any], email: str | None) -> str | None:
    meta = claims.get("user_metadata")
    if isinstance(meta, dict):  # display only; never used for authorization
        for key in ("display_name", "full_name", "name"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:100]
    return email.split("@")[0][:100] if email else None


def _is_google(app_metadata: dict[str, Any]) -> bool:
    """app_metadata.provider/providers are set by Supabase Auth itself (not user-writable)."""
    providers = app_metadata.get("providers")
    return app_metadata.get("provider") == "google" or (isinstance(providers, list) and "google" in providers)


def authenticate_token(token: str, settings: Settings) -> CurrentUser:
    """Verify a Supabase access token and return the user. Raises Unauthenticated / Forbidden /
    ProviderUnavailable."""
    if not settings.supabase_url:
        raise ProviderUnavailable("Authentication provider is not configured.")
    try:
        signing_key = get_jwks_client(settings.supabase_jwks_url).get_signing_key_from_jwt(token)  # type: ignore[arg-type]
    except PyJWKClientConnectionError as exc:
        log.error("JWKS fetch failed: %s", type(exc).__name__)
        raise ProviderUnavailable("Authentication provider is unreachable.") from exc
    except (PyJWKClientError, jwt.PyJWTError) as exc:  # malformed token, unknown kid, unsupported alg...
        raise Unauthenticated("Invalid or expired token.") from exc
    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=ALGORITHMS,
            audience=AUDIENCE,
            issuer=settings.supabase_issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "sub"]},
        )
        user_id = uuid.UUID(str(claims["sub"]))
    except (jwt.PyJWTError, ValueError) as exc:
        raise Unauthenticated("Invalid or expired token.") from exc

    app_metadata = claims.get("app_metadata")
    app_metadata = app_metadata if isinstance(app_metadata, dict) else {}
    role = app_metadata.get(ROLE_CLAIM)
    if role is None and settings.google_default_role and _is_google(app_metadata):
        role = settings.google_default_role  # owner decision 2026-11-12: Google sign-in = read-only observer
    if role not in ROLES:  # missing, wrong type or unknown value; user_metadata is deliberately ignored
        raise Forbidden("No Anticipa role assigned")
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    return CurrentUser(id=user_id, email=email, role=role, display_name=_display_name(claims, email))


# --- user_profiles upsert (best effort, throttled per process)
# ---------------------------------------------------

_last_touch: dict[uuid.UUID, float] = {}
_touch_lock = threading.Lock()


def touch_profile(sessions: sessionmaker[Session], user: CurrentUser, min_interval: float) -> None:
    """Upsert the user's profile at most once per `min_interval` seconds per process. Never fails the
    request."""
    now = time.monotonic()
    with _touch_lock:
        last = _last_touch.get(user.id)
        if last is not None and now - last < min_interval:
            return
        if len(_last_touch) > 10_000:
            _last_touch.clear()
        _last_touch[user.id] = now
    try:
        with sessions() as s:
            stmt = pg_insert(UserProfile).values(
                id=user.id, email=user.email, display_name=user.display_name, last_seen_at=func.now()
            )
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "email": stmt.excluded.email,
                        "display_name": stmt.excluded.display_name,
                        "last_seen_at": func.now(),
                    },
                )
            )
            s.commit()
    except Exception:
        with _touch_lock:
            _last_touch.pop(user.id, None)  # retry on the next request
        log.warning("user_profiles upsert failed", exc_info=True)


# --- FastAPI dependencies
# ------------------------------------------------------------------------------------------


def get_current_user(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> CurrentUser:
    if credentials is None or not credentials.credentials:
        raise Unauthenticated("A bearer token is required.")
    settings: Settings = request.app.state.settings
    user = authenticate_token(credentials.credentials, settings)
    touch_profile(request.app.state.sessionmaker, user, settings.profile_touch_seconds)
    request.state.user = user
    return user


def require_permission(permission: str) -> Callable[..., CurrentUser]:
    """Dependency factory: `Depends(require_permission("jobs.view"))` -> CurrentUser, else 401/403."""

    def dependency(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        if not user.can(permission):
            log.warning(
                "permission denied",
                extra={"user_id": str(user.id), "role": user.role, "permission": permission},
            )
            raise Forbidden()
        return user

    dependency.__name__ = f"require_{permission.replace('.', '_')}"
    return dependency


def require_any_permission(*permissions: str) -> Callable[..., CurrentUser]:
    """Like require_permission, satisfied by any one of `permissions` (callers branch on `user.can(...)`)."""

    def dependency(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        if not any(user.can(p) for p in permissions):
            log.warning(
                "permission denied",
                extra={"user_id": str(user.id), "role": user.role, "permission": "|".join(permissions)},
            )
            raise Forbidden()
        return user

    return dependency
