"""Audit history API (05 §4.4). `audit.view`: full history. `audit.view_scoped`: only the caller's own events.
`audit.view_redacted` (observer): every event, but without actor ids, request ids or free-form details."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from anticipa.api.deps import PageDep, SessionDep
from anticipa.api.envelope import PagedEnvelope, paged, paginate
from anticipa.core.auth import CurrentUser, require_any_permission
from anticipa.core.models import AuditEvent
from anticipa.core.rbac import AUDIT_VIEW, AUDIT_VIEW_REDACTED, AUDIT_VIEW_SCOPED

router = APIRouter(prefix="/audit", tags=["audit"])
AuditViewer = Annotated[
    CurrentUser, Depends(require_any_permission(AUDIT_VIEW, AUDIT_VIEW_SCOPED, AUDIT_VIEW_REDACTED))
]


class AuditEventOut(BaseModel):
    id: uuid.UUID
    occurred_at: datetime
    actor_id: uuid.UUID | None
    actor_role: str | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    request_id: str | None
    details: dict[str, Any]


def to_out(event: AuditEvent, *, redact: bool) -> AuditEventOut:
    out = AuditEventOut.model_validate(event, from_attributes=True)
    if redact:  # role stays (who kind of actor), identity and free-form content go
        out.actor_id, out.request_id, out.details = None, None, {}
    return out


@router.get("", response_model=PagedEnvelope[AuditEventOut], summary="Audit events (newest first)")
def list_audit(
    request: Request,
    session: SessionDep,
    params: PageDep,
    user: AuditViewer,
    action: Annotated[str | None, Query(max_length=100)] = None,
    resource_type: Annotated[str | None, Query(max_length=100)] = None,
    resource_id: Annotated[str | None, Query(max_length=200)] = None,
    outcome: Annotated[str | None, Query(pattern="^(success|failure|denied)$")] = None,
):
    stmt = select(AuditEvent).order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
    full = user.can(AUDIT_VIEW)
    redact = not full and not user.can(AUDIT_VIEW_SCOPED)  # only audit.view_redacted
    if not full and not redact:  # scoped roles: own actions only
        stmt = stmt.where(AuditEvent.actor_id == user.id)
    for column, value in (
        (AuditEvent.action, action),
        (AuditEvent.resource_type, resource_type),
        (AuditEvent.resource_id, resource_id),
        (AuditEvent.outcome, outcome),
    ):
        if value:
            stmt = stmt.where(column == value)
    rows, total = paginate(session, stmt, params)
    return paged(request, [to_out(r, redact=redact) for r in rows], params, total)
