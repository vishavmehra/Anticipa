"""Engagement APIs (05 §14-15, §16): outreach drafts, SIMULATED communication events, resident responses, the per-case
communication history, appointments (from confirmed allocations) and the engagement overview.

Read endpoints need engagement.view / appointments.view / analytics.view (the read-only `observer` role has them).
Everything that records or changes something needs caregaps.manage (there is no separate engagement.manage permission);
starting the simulation needs processing.execute. Every payload says it is simulated and every mutation is audited.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, computed_field
from sqlalchemy import select
from sqlalchemy.orm import Session

from anticipa.api.deps import PageDep, SessionDep
from anticipa.api.envelope import Envelope, PagedEnvelope, ok, paged, paginate
from anticipa.api.jobs import JobOut, to_out
from anticipa.caregaps import service as caregaps
from anticipa.core import audit
from anticipa.core.auth import CurrentUser, require_any_permission, require_permission
from anticipa.core.rbac import (
    ANALYTICS_VIEW,
    ANALYTICS_VIEW_LIMITED,
    APPOINTMENTS_VIEW,
    CAREGAPS_MANAGE,
    ENGAGEMENT_VIEW,
    PROCESSING_EXECUTE,
)
from anticipa.engagement import service
from anticipa.engagement.models import (
    APPOINTMENT_STATUSES,
    CHANNELS,
    DELIVERY_STATES,
    LANGUAGES,
    RESPONSE_TYPES,
    Appointment,
    CommunicationEvent,
    ResidentResponse,
)
from anticipa.engagement.simulate import SimScope
from anticipa.engagement.templates import DIRECTION
from anticipa.jobs import service as jobs_service
from anticipa.planning.models import Clinic, ClinicService

engagement = APIRouter(prefix="/engagement", tags=["engagement"])
case_history = APIRouter(prefix="/care-gaps", tags=["engagement"])
appointments = APIRouter(prefix="/appointments", tags=["appointments"])
analytics = APIRouter(prefix="/analytics", tags=["analytics"])
router = APIRouter()  # the module's single export (core.modules loads `router`)

ViewUser = Annotated[CurrentUser, Depends(require_permission(ENGAGEMENT_VIEW))]
ApptViewUser = Annotated[CurrentUser, Depends(require_permission(APPOINTMENTS_VIEW))]
ManageUser = Annotated[CurrentUser, Depends(require_permission(CAREGAPS_MANAGE))]
ExecuteUser = Annotated[CurrentUser, Depends(require_permission(PROCESSING_EXECUTE))]
AnalyticsUser = Annotated[CurrentUser, Depends(require_any_permission(ANALYTICS_VIEW, ANALYTICS_VIEW_LIMITED))]
SIMULATED_NOTE = "Simulated: no message was sent to a real person and no real booking exists."


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------------------------------------- schemas
class DraftIn(_In):
    care_gap_case_id: uuid.UUID
    language: Literal[*LANGUAGES] | None = None  # type: ignore[valid-type]
    template_key: str | None = Field(None, max_length=64)


class DraftOut(BaseModel):
    care_gap_case_id: uuid.UUID
    resident_id: uuid.UUID
    care_activity: str
    template_key: str
    template_version: int
    language: str
    language_source: str
    direction: str
    body: str
    channels: list[str]
    constraints_checked: bool
    simulated: bool
    synthetic_status: str
    notice: str


class EventIn(_In):
    care_gap_case_id: uuid.UUID
    channel: Literal[*CHANNELS]  # type: ignore[valid-type]
    delivery_state: Literal[*DELIVERY_STATES] = "queued"  # type: ignore[valid-type]
    template_key: str | None = Field(None, max_length=64)
    language: Literal[*LANGUAGES] | None = None  # type: ignore[valid-type]


class DeliveryIn(_In):
    delivery_state: Literal["sent", "delivered", "failed"]


class ResponseIn(_In):
    communication_event_id: uuid.UUID
    response_type: Literal[*RESPONSE_TYPES]  # type: ignore[valid-type]
    response_value: str | None = Field(None, max_length=1000)
    source_reference: str | None = Field(None, max_length=200)


class ResponseOut(_Out):
    id: uuid.UUID
    communication_event_id: uuid.UUID
    care_gap_case_id: uuid.UUID
    resident_id: uuid.UUID
    response_type: str
    response_value: str | None
    received_at: datetime
    source_reference: str | None
    created_at: datetime
    synthetic_status: str
    simulated: bool = True


class EventOut(_Out):
    id: uuid.UUID
    care_gap_case_id: uuid.UUID
    resident_id: uuid.UUID
    channel: str
    template_key: str
    template_version: int
    language: str
    body: str
    delivery_state: str
    source_type: str
    job_id: uuid.UUID | None
    sent_at: datetime | None
    delivered_at: datetime | None
    response_received_at: datetime | None
    created_by: uuid.UUID | None
    created_at: datetime
    synthetic_status: str
    simulated: bool = True
    notice: str = SIMULATED_NOTE

    @computed_field  # type: ignore[prop-decorator]
    @property
    def direction(self) -> str:
        return DIRECTION.get(self.language, "ltr")


class CommunicationOut(EventOut):
    responses: list[ResponseOut] = []


class SimulationIn(SimScope):
    idempotency_key: str | None = Field(None, max_length=200)


class AppointmentIn(_In):
    planned_allocation_id: uuid.UUID
    appointment_status: Literal["proposed", "simulated_confirmed"] = "proposed"


class AppointmentStatusIn(_In):
    appointment_status: Literal[*APPOINTMENT_STATUSES]  # type: ignore[valid-type]
    reason: str | None = Field(None, max_length=1000)


class AppointmentOut(_Out):
    id: uuid.UUID
    care_gap_case_id: uuid.UUID
    resident_id: uuid.UUID
    clinic_id: uuid.UUID
    clinic_service_id: uuid.UUID
    capacity_slot_id: uuid.UUID
    planned_allocation_id: uuid.UUID
    clinic_key: str | None = None
    service_key: str | None = None
    start_time: datetime
    end_time: datetime
    appointment_status: str
    status_reason: str | None
    source_type: str
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    synthetic_status: str
    simulated: bool = True
    notice: str = SIMULATED_NOTE


def _appointments_out(session: Session, rows: list[Appointment]) -> list[AppointmentOut]:
    keys: dict[uuid.UUID, tuple[str, str]] = {}
    if rows:
        keys = {
            r.id: (r.clinic_key, r.service_key)
            for r in session.execute(
                select(Appointment.id, Clinic.clinic_key, ClinicService.service_key)
                .join(Clinic, Clinic.id == Appointment.clinic_id)
                .join(ClinicService, ClinicService.id == Appointment.clinic_service_id)
                .where(Appointment.id.in_([a.id for a in rows]))
            )  # fmt: skip
        }
    out = []
    for a in rows:
        item = AppointmentOut.model_validate(a)
        item.clinic_key, item.service_key = keys.get(a.id, (None, None))
        out.append(item)
    return out


# ----------------------------------------------------------------------------------------------- drafts / events / responses
@engagement.post("/drafts", response_model=Envelope[DraftOut],
                 summary="Render the approved outreach template for a case (nothing is sent)")  # fmt: skip
def create_draft(body: DraftIn, request: Request, session: SessionDep, user: ManageUser):
    case = caregaps.get_case(session, body.care_gap_case_id)
    service.check_case_scope(user, case)
    data = service.draft(session, case, template_key=body.template_key, language=body.language)
    session.commit()  # stores the approved template rows on first use
    return ok(request, data)


@engagement.post("/events", response_model=Envelope[EventOut], status_code=201,
                 summary="Record a SIMULATED outreach message for a planned case")  # fmt: skip
def create_event(body: EventIn, request: Request, session: SessionDep, user: ManageUser):
    case = caregaps.get_case(session, body.care_gap_case_id)
    event = service.record_event(
        session, case, channel=body.channel, delivery_state=body.delivery_state, template_key=body.template_key,
        language=body.language, actor=user.actor, user=user,
    )  # fmt: skip
    session.commit()
    return ok(request, EventOut.model_validate(event))


@engagement.get("/events", response_model=PagedEnvelope[EventOut], summary="List simulated communication events (newest first)")
def list_events(
    request: Request, session: SessionDep, user: ViewUser, params: PageDep, care_gap_case_id: uuid.UUID | None = None,
    resident_id: uuid.UUID | None = None, channel: Annotated[Literal[*CHANNELS] | None, Query()] = None,  # type: ignore[valid-type]
    delivery_state: Annotated[Literal[*DELIVERY_STATES] | None, Query()] = None,  # type: ignore[valid-type]
):  # fmt: skip
    stmt = select(CommunicationEvent).order_by(CommunicationEvent.created_at.desc(), CommunicationEvent.id.desc())
    for column, value in ((CommunicationEvent.care_gap_case_id, care_gap_case_id), (CommunicationEvent.resident_id, resident_id),
                          (CommunicationEvent.channel, channel), (CommunicationEvent.delivery_state, delivery_state)):  # fmt: skip
        if value is not None:
            stmt = stmt.where(column == value)
    rows, total = paginate(session, stmt, params)
    return paged(request, [EventOut.model_validate(r) for r in rows], params, total)


@engagement.get("/events/{event_id}", response_model=Envelope[CommunicationOut], summary="One communication event with its responses")
def get_event(event_id: uuid.UUID, request: Request, session: SessionDep, user: ViewUser):
    event = service.get_event(session, event_id)
    return ok(request, _communication(event, list(session.execute(
        select(ResidentResponse).where(ResidentResponse.communication_event_id == event.id)
        .order_by(ResidentResponse.received_at, ResidentResponse.id)).scalars())))  # fmt: skip


@engagement.patch("/events/{event_id}/delivery-state", response_model=Envelope[EventOut],
                  summary="Progress a simulated message: queued -> sent -> delivered | failed")  # fmt: skip
def update_delivery(event_id: uuid.UUID, body: DeliveryIn, request: Request, session: SessionDep, user: ManageUser):
    event = service.get_event(session, event_id)
    service.set_delivery_state(session, event, body.delivery_state, actor=user.actor, user=user)
    session.commit()
    return ok(request, EventOut.model_validate(event))


@engagement.post("/responses", response_model=Envelope[ResponseOut], status_code=201,
                 summary="Record a SIMULATED resident response (a question is escalated to a clinician)")  # fmt: skip
def create_response(body: ResponseIn, request: Request, session: SessionDep, user: ManageUser):
    event = service.get_event(session, body.communication_event_id)
    response = service.record_response(
        session, event, response_type=body.response_type, response_value=body.response_value,
        source_reference=body.source_reference, actor=user.actor, user=user,
    )  # fmt: skip
    session.commit()
    return ok(request, ResponseOut.model_validate(response))


@engagement.post("/simulations", response_model=Envelope[JobOut], status_code=202,
                 summary="Run the simulated engagement + closure loop for planned cases (job, seed-deterministic)")  # fmt: skip
def submit_simulation(body: SimulationIn, request: Request, response: Response, session: SessionDep, user: ExecuteUser):
    scope = body.model_dump(mode="json", exclude={"idempotency_key"})
    key = body.idempotency_key or request.headers.get("Idempotency-Key")
    result = jobs_service.submit_job(session, "engagement.simulate", scope, key, user.id)
    if result.created:
        audit.record(session, user.actor, "engagement.simulation_submit", "processing_job", result.job.id, "success",
                     {**scope, "simulated": True})  # fmt: skip
    session.commit()
    response.status_code = 202 if result.created else 200
    return ok(request, to_out(result.job, user))


def _communication(event: CommunicationEvent, responses: list[ResidentResponse]) -> CommunicationOut:
    out = CommunicationOut.model_validate(event)
    out.responses = [ResponseOut.model_validate(r) for r in responses]
    return out


@case_history.get("/{case_id}/communications", response_model=Envelope[list[CommunicationOut]],
                  summary="Communication history of a case (simulated messages with their responses)")  # fmt: skip
def case_communications(case_id: uuid.UUID, request: Request, session: SessionDep, user: ViewUser):
    caregaps.get_case(session, case_id)  # 404 for an unknown case
    return ok(request, [_communication(e, r) for e, r in service.communications_for_case(session, case_id)])


# ----------------------------------------------------------------------------------------------- appointments
@appointments.post("", response_model=Envelope[AppointmentOut], status_code=201,
                   summary="Create the appointment record from a CONFIRMED allocation: holds the slot (proposed) and moves the case to planned")  # fmt: skip
def create_appointment(body: AppointmentIn, request: Request, session: SessionDep, user: ManageUser):
    appt = service.create_appointment(session, body.planned_allocation_id, body.appointment_status, actor=user.actor, user=user)
    session.commit()
    return ok(request, _appointments_out(session, [appt])[0])


@appointments.get("", response_model=PagedEnvelope[AppointmentOut], summary="List appointments (soonest first)")
def list_appointments(
    request: Request, session: SessionDep, user: ApptViewUser, params: PageDep,
    appointment_status: Annotated[list[Literal[*APPOINTMENT_STATUSES]] | None, Query()] = None,  # type: ignore[valid-type]
    care_gap_case_id: uuid.UUID | None = None, resident_id: uuid.UUID | None = None, clinic_id: uuid.UUID | None = None,
):  # fmt: skip
    stmt = select(Appointment).order_by(Appointment.start_time, Appointment.id)
    if appointment_status:
        stmt = stmt.where(Appointment.appointment_status.in_(appointment_status))
    for column, value in ((Appointment.care_gap_case_id, care_gap_case_id), (Appointment.resident_id, resident_id),
                          (Appointment.clinic_id, clinic_id)):  # fmt: skip
        if value is not None:
            stmt = stmt.where(column == value)
    rows, total = paginate(session, stmt, params)
    return paged(request, _appointments_out(session, rows), params, total)


@appointments.get("/{appointment_id}", response_model=Envelope[AppointmentOut], summary="One appointment")
def get_appointment(appointment_id: uuid.UUID, request: Request, session: SessionDep, user: ApptViewUser):
    return ok(request, _appointments_out(session, [service.get_appointment(session, appointment_id)])[0])


@appointments.patch("/{appointment_id}/status", response_model=Envelope[AppointmentOut],
                    summary="proposed -> simulated_confirmed -> completed | no_show | cancelled (never completes the case)")  # fmt: skip
def update_appointment_status(appointment_id: uuid.UUID, body: AppointmentStatusIn, request: Request, session: SessionDep,
                              user: ManageUser):  # fmt: skip
    appt = service.get_appointment(session, appointment_id)
    service.set_appointment_status(session, appt, body.appointment_status, reason=body.reason, actor=user.actor, user=user)
    session.commit()
    return ok(request, _appointments_out(session, [appt])[0])


# ----------------------------------------------------------------------------------------------- analytics
@analytics.get("/engagement", summary="Engagement overview: counts by state, labelled simulated")
def engagement_overview(request: Request, session: SessionDep, user: AnalyticsUser):
    return ok(request, service.engagement_overview(session))


router.include_router(engagement)
router.include_router(case_history)
router.include_router(appointments)
router.include_router(analytics)
