# ruff: noqa: E501
"""Resident conversation API (ADR-071), mounted under /api/v1.

  GET  /care-gaps/{case_id}/conversation           the simulated thread (engagement.view; the observer role holds it)
  POST /care-gaps/{case_id}/conversation/messages  record a simulated resident message and get the reply (caregaps.manage)

Every payload says it is simulated. A medical question, an emergency or an eligibility dispute never gets a model answer: the
approved hand off text is returned and the case is escalated through the case lifecycle. A proposed slot is a suggestion only;
nothing is booked without a coordinator using the appointment flow. Every message writes one `conversation.message` audit
event (a hash of the text, never the text).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, computed_field

from anticipa.api.deps import SessionDep
from anticipa.api.envelope import Envelope, ok
from anticipa.caregaps import service as caregaps
from anticipa.consent import service as consent
from anticipa.conversation import service
from anticipa.core.auth import CurrentUser, require_permission
from anticipa.core.rbac import CAREGAPS_MANAGE, ENGAGEMENT_VIEW
from anticipa.datafabric.models import Resident
from anticipa.engagement.models import CHANNELS, LANGUAGES
from anticipa.engagement.templates import DIRECTION, SIMULATION_MARKERS

router = APIRouter(prefix="/care-gaps", tags=["conversation"])
ViewUser = Annotated[CurrentUser, Depends(require_permission(ENGAGEMENT_VIEW))]
ManageUser = Annotated[CurrentUser, Depends(require_permission(CAREGAPS_MANAGE))]
NOTICE = "Simulated conversation: no message was sent to a real person and nothing was booked."


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=600)
    language: Literal[*LANGUAGES] | None = None  # type: ignore[valid-type]  (language the coordinator set for the resident)
    channel: Literal[*CHANNELS] | None = None  # type: ignore[valid-type]  (channel the resident wrote on; default: the last one used on the case)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    body: str
    language: str
    intent: str | None
    source: str
    escalation_state: str | None
    escalation_reason: str | None
    proposal: dict[str, Any] | None
    model_id: str | None
    created_at: datetime
    synthetic_status: str
    simulated: bool = True
    reply_sent: bool | None = None  # resident messages only: False when outreach on the channel was denied and no reply was sent

    @computed_field  # type: ignore[prop-decorator]
    @property
    def direction(self) -> str:
        return DIRECTION.get(self.language, "ltr")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def marker(self) -> str | None:
        return SIMULATION_MARKERS.get(self.language) if self.role == "assistant" else None


class ThreadOut(BaseModel):
    care_gap_case_id: uuid.UUID
    case_status: str
    resident_language: str | None
    escalated: bool
    consent: dict[str, Any]  # what the consent ledger says now: channels the resident denied, whether outreach is blocked
    messages: list[MessageOut]
    simulated: bool = True
    notice: str = NOTICE


class PostOut(BaseModel):
    resident_message: MessageOut
    reply: MessageOut | None
    suppressed: bool
    suppressed_reason: str | None  # consent_denied: the resident opted out of this channel, no reply was sent
    consent_override: str | None  # emergency_safety_reply: the urgent care reply was sent although the channel is denied
    channel: str
    escalation: dict[str, Any] | None
    opt_out: dict[str, Any] | None
    case_status: str
    cost_usd: float
    latency_ms: int
    simulated: bool = True
    notice: str = NOTICE


@router.get("/{case_id}/conversation", response_model=Envelope[ThreadOut], summary="The simulated conversation thread of a case")
def get_conversation(case_id: uuid.UUID, request: Request, session: SessionDep, user: ViewUser):
    case = caregaps.get_case(session, case_id)
    resident = session.get(Resident, case.resident_id)
    rows = service.thread_rows(session, case_id)
    state = consent.state_for(session, case.resident_id)
    messages = [MessageOut.model_validate(r) for r in rows]
    for i, m in enumerate(messages):
        if m.role == "resident":
            m.reply_sent = i + 1 < len(messages) and messages[i + 1].role == "assistant"
    return ok(request, ThreadOut(
        care_gap_case_id=case.id, case_status=case.status, resident_language=resident.preferred_language if resident else None,
        escalated=case.status == "escalated", messages=messages,
        consent={"denied_channels": sorted(state.denied_channels), "outreach_blocked": state.outreach_blocked, "ai_processing_denied": state.ai_processing_denied},
    ))  # fmt: skip


@router.post("/{case_id}/conversation/messages", response_model=Envelope[PostOut], status_code=201,
             summary="Record a SIMULATED resident message; the reply is logistics only and any health matter is escalated")  # fmt: skip
async def post_message(case_id: uuid.UUID, body: MessageIn, request: Request, session: SessionDep, user: ManageUser):
    case = caregaps.get_case(session, case_id)
    out = await service.post_message(session, user, case, body.text, body.language, channel=body.channel)
    reply = out.reply
    return ok(request, PostOut(
        resident_message=MessageOut.model_validate(out.resident).model_copy(update={"reply_sent": out.assistant is not None}),
        reply=MessageOut.model_validate(out.assistant) if out.assistant else None,
        suppressed=reply.suppressed, suppressed_reason="consent_denied" if reply.suppressed else None,
        consent_override="emergency_safety_reply" if reply.consent_override else None, channel=out.channel,
        escalation=({"reason": reply.escalation, "state": out.escalation_state, "urgent": reply.escalation == "emergency"} if reply.escalation else None),
        opt_out=({"detected": True, "consent_recorded": out.consent_record_id is not None, "consent_record_id": out.consent_record_id} if reply.opt_out else None),
        case_status=out.case_status, cost_usd=float(reply.cost_usd), latency_ms=reply.latency_ms,
    ))  # fmt: skip
