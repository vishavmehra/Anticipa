"""Append-only audit recording. `record()` adds to the caller's transaction (caller commits)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from anticipa.core.logging import request_id_var
from anticipa.core.models import AuditEvent

OUTCOMES = ("success", "failure", "denied")


@dataclass(frozen=True)
class Actor:
    id: uuid.UUID | None
    role: str | None

    @classmethod
    def system(cls) -> Actor:
        return cls(None, "system")


def record(
    session: Session,
    actor: Actor | None,
    action: str,
    resource_type: str,
    resource_id: str | uuid.UUID | None = None,
    outcome: str = "success",
    details: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
) -> AuditEvent:
    """Stage an audit event in `session` (flushed, not committed). `details` must never hold secrets or PHI
    text."""
    if outcome not in OUTCOMES:
        raise ValueError(f"invalid audit outcome: {outcome!r}")
    actor = actor or Actor.system()
    event = AuditEvent(
        actor_id=actor.id,
        actor_role=actor.role,
        action=action,
        resource_type=resource_type,
        resource_id=None if resource_id is None else str(resource_id),
        outcome=outcome,
        request_id=request_id or request_id_var.get(),
        details=details or {},
    )
    session.add(event)
    session.flush()
    return event
