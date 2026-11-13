"""Demo-user provisioning through the Supabase Auth Admin API (ADR-057/060). Used by `anticipa seed-users`.

Idempotent create-or-update by email: the user is confirmed (`email_confirm`), gets the demo password and its
Anticipa role in
`app_metadata.anticipa_role` (only the secret key can write app_metadata; the API trusts nothing else). The
secret key, the
password and tokens are never logged or returned.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session

from anticipa.config import Settings
from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.core.rbac import Role

log = logging.getLogger(__name__)

# (email, role, display name). Example-domain addresses on purpose: they can never receive real mail.
DEMO_USERS: tuple[tuple[str, Role, str], ...] = (
    ("anticipa.judge@example.com", Role.OBSERVER, "Judge (read-only)"),
    ("anticipa.admin@example.com", Role.POPULATION_ADMIN, "Population Admin"),
    ("anticipa.coordinator@example.com", Role.CARE_COORDINATOR, "Care Coordinator"),
    ("anticipa.reviewer@example.com", Role.CLINICAL_REVIEWER, "Clinical Reviewer"),
    ("anticipa.sysadmin@example.com", Role.SYSTEM_ADMIN, "System Admin"),
)
MIN_PASSWORD_LENGTH = 8
PAGE_SIZE = 200
MAX_PAGES = 50  # 10,000 users: far beyond a demo project; the bound only prevents an endless loop
_PLACEHOLDER_MARKERS = ("replace-me",)


class SeedError(RuntimeError):
    """An actionable failure (missing configuration or a Supabase error). Messages never contain secrets."""


@dataclass(frozen=True)
class SeedResult:
    email: str
    role: str
    action: str  # created | updated
    user_id: str


def _require(settings: Settings, password: str | None) -> tuple[str, str, str]:
    if not settings.supabase_url:
        raise SeedError("SUPABASE_URL is not set (repo-root .env).")
    secret = settings.supabase_secret_key.get_secret_value().strip() if settings.supabase_secret_key else ""
    if not secret or any(m in secret for m in _PLACEHOLDER_MARKERS):
        raise SeedError(
            "SUPABASE_SECRET_KEY is empty. Paste the secret key (Supabase dashboard -> Project Settings -> "
            "API Keys) into the repo-root .env; it is server-side only and never reaches the frontend."
        )
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise SeedError(
            f"ANTICIPA_DEMO_PASSWORD must be set (at least {MIN_PASSWORD_LENGTH} characters) in the "
            "repo-root .env."
        )
    return settings.supabase_url, secret, password


def _fail(action: str, response: httpx.Response) -> SeedError:
    try:
        body = response.json()
        reason = str(
            body.get("msg") or body.get("message") or body.get("error_description") or body.get("error") or ""
        )
    except ValueError:
        reason = ""
    suffix = f": {reason[:200]}" if reason else ""
    return SeedError(f"Supabase Admin API: {action} failed (HTTP {response.status_code}){suffix}.")


def _existing_users(client: httpx.Client, base: str) -> dict[str, str]:
    """email (lowercase) -> user id, over all pages of the Admin API listing."""
    found: dict[str, str] = {}
    for page in range(1, MAX_PAGES + 1):
        resp = client.get(f"{base}/auth/v1/admin/users", params={"page": page, "per_page": PAGE_SIZE})
        if resp.status_code != 200:
            raise _fail("listing users", resp)
        users = resp.json().get("users", [])
        for user in users:
            if user.get("email"):
                found[str(user["email"]).lower()] = str(user["id"])
        if len(users) < PAGE_SIZE:
            break
    return found


def seed_users(
    settings: Settings,
    password: str | None,
    *,
    users: tuple[tuple[str, Role, str], ...] = DEMO_USERS,
    transport: httpx.BaseTransport | None = None,
    judge_password: str | None = None,
) -> list[SeedResult]:
    """Create or update every demo user. `transport` lets tests mock the Admin API (never call Supabase from
    tests)."""
    base, secret, password = _require(settings, password)
    if judge_password is not None and len(judge_password) < MIN_PASSWORD_LENGTH:
        raise SeedError(f"ANTICIPA_JUDGE_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters.")
    if judge_password == password:
        judge_password = None  # identical is the same as unset: warn below
    if judge_password is None:
        log.warning(
            "every demo user shares one password: set ANTICIPA_JUDGE_PASSWORD so the shareable judge login "
            "cannot sign in as a privileged demo user"
        )
    headers = {"apikey": secret, "Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
    results: list[SeedResult] = []
    try:
        with httpx.Client(headers=headers, timeout=20.0, transport=transport) as client:
            existing = _existing_users(client, base)
            for email, role, display_name in users:
                payload: dict[str, Any] = {
                    "email": email,
                    "password": judge_password if role is Role.OBSERVER and judge_password else password,
                    "email_confirm": True,
                    "app_metadata": {"anticipa_role": role.value},
                    "user_metadata": {"display_name": display_name},
                }
                user_id = existing.get(email.lower())
                if user_id is None:
                    resp = client.post(f"{base}/auth/v1/admin/users", json=payload)
                    action, verb = "created", "create"
                else:
                    resp = client.put(f"{base}/auth/v1/admin/users/{user_id}", json=payload)
                    action, verb = "updated", "update"
                if resp.status_code not in (200, 201):
                    raise _fail(f"{verb} of {email}", resp)
                results.append(SeedResult(email, role.value, action, str(resp.json().get("id", user_id))))
    except httpx.HTTPError as exc:
        raise SeedError(f"Supabase Admin API is unreachable ({type(exc).__name__}).") from exc
    return results


def record_seed_audit(session: Session, results: list[SeedResult]) -> None:
    """One audit event per provisioned user (role assignment is a sensitive action). Caller commits."""
    for r in results:
        audit.record(
            session,
            Actor.system(),
            f"auth.user_{r.action}",
            "user",
            r.user_id,
            "success",
            {"role": r.role, "via": "seed-users"},
        )
