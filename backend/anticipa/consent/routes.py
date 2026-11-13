"""Consent APIs (ADR-070): a resident's permit/deny decisions for outreach and AI processing.

GET needs `consent.view` (every role incl. the read-only observer); POST needs `consent.manage`
(care_coordinator and population_admin). Records are append-only: withdrawing a consent is recording the
opposite status. Every payload is labelled synthetic/simulated and every mutation and read is audited.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from anticipa.api.deps import SessionDep
from anticipa.api.envelope import Envelope, ok
from anticipa.consent import service
from anticipa.consent.models import SCOPES, STATUSES, ConsentRecord
from anticipa.core import audit
from anticipa.core.auth import CurrentUser, require_permission
from anticipa.core.errors import NotFound
from anticipa.core.rbac import CONSENT_MANAGE, CONSENT_VIEW
from anticipa.datafabric.models import Resident
from anticipa.engagement.models import CHANNELS, LANGUAGES

router = APIRouter(prefix="/residents", tags=["consent"])

ViewUser = Annotated[CurrentUser, Depends(require_permission(CONSENT_VIEW))]
ManageUser = Annotated[CurrentUser, Depends(require_permission(CONSENT_MANAGE))]
NOTICE = "Synthetic residents and simulated decisions: no real person's consent is recorded here."
DEFAULT_NOTE = "No decision on file means permit for outreach and AI processing (synthetic default)."


class ConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal[*SCOPES]  # type: ignore[valid-type]
    status: Literal[*STATUSES]  # type: ignore[valid-type]
    channel: Literal[*CHANNELS] | None = None  # type: ignore[valid-type]  # outreach only; omitted = every channel
    language: Literal[*LANGUAGES] | None = None  # type: ignore[valid-type]
    source: Literal["resident_request", "coordinator_entry"] = "coordinator_entry"
    effective_to: AwareDatetime | None = None  # a time-limited decision; omitted = until changed
    note: str | None = Field(None, max_length=service.NOTE_MAX)  # short and without identifiers


class ConsentOut(BaseModel):
    id: uuid.UUID
    resident_id: uuid.UUID
    scope: str
    status: str
    channel: str | None
    language: str | None
    source: str
    effective_from: datetime
    effective_to: datetime | None
    recorded_by: uuid.UUID | None
    note: str | None
    synthetic_status: str
    created_at: datetime
    in_force: bool  # the newest currently effective record of its scope and channel
    fhir: dict[str, Any]  # FHIR R4 Consent shape of this record


class OutreachState(BaseModel):
    permitted_channels: list[str]
    denied_channels: list[str]
    blocked: bool  # every channel denied: planning leaves the resident's cases unallocated (consent_denied)


class EffectiveConsent(BaseModel):
    outreach: OutreachState
    data_use_ai_processing: Literal["permit", "deny"]


class ConsentView(BaseModel):
    resident_id: uuid.UUID
    effective: EffectiveConsent
    records: list[ConsentOut]
    default_note: str = DEFAULT_NOTE
    notice: str = NOTICE


def _view(resident_id: uuid.UUID, records: list[ConsentRecord], state: service.ConsentState) -> ConsentView:
    active = set(state.in_force)
    return ConsentView(
        resident_id=resident_id,
        effective=EffectiveConsent(
            outreach=OutreachState(
                permitted_channels=list(state.permitted_channels),
                denied_channels=[c for c in CHANNELS if c in state.denied_channels],
                blocked=state.outreach_blocked,
            ),
            data_use_ai_processing="deny" if state.ai_processing_denied else "permit",
        ),
        records=[
            ConsentOut(
                **{c: getattr(r, c) for c in ConsentOut.model_fields if hasattr(r, c)},
                note=(r.provenance or {}).get("note"),
                in_force=r.id in active,
                fhir=service.to_fhir(r, in_force=r.id in active),
            )
            for r in records
        ],
    )


def _resident_or_404(session, resident_id: uuid.UUID) -> None:
    if session.get(Resident, resident_id) is None:
        raise NotFound("Resident not found.")


@router.get(
    "/{resident_id}/consents",
    response_model=Envelope[ConsentView],
    summary="Consent decisions of a resident (newest first) and what is in force",
)
def get_consents(resident_id: uuid.UUID, request: Request, session: SessionDep, user: ViewUser):
    _resident_or_404(session, resident_id)
    records, state = service.list_records(session, resident_id)
    audit.record(
        session,
        user.actor,
        "resident.consent.read",
        "resident",
        resident_id,
        "success",
        {"records": len(records)},
    )
    session.commit()
    return ok(request, _view(resident_id, records, state))


@router.post(
    "/{resident_id}/consents",
    response_model=Envelope[ConsentView],
    status_code=201,
    summary="Record a consent decision (append-only; withdraw by recording the opposite status)",
)
def create_consent(
    resident_id: uuid.UUID, body: ConsentIn, request: Request, session: SessionDep, user: ManageUser
):
    service.record_consent(
        session,
        resident_id,
        scope=body.scope,
        status=body.status,
        channel=body.channel,
        language=body.language,
        source=body.source,
        effective_to=body.effective_to,
        note=body.note,
        actor=user.actor,
    )
    session.commit()
    records, state = service.list_records(session, resident_id)
    return ok(request, _view(resident_id, records, state))
