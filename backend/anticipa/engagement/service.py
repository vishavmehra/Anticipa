"""Engagement workflows over the database: outreach drafts, SIMULATED communication events, resident responses,
appointments and the engagement overview. Routes and the simulation job stay thin and call these functions.

Rules (CLAUDE.md §10, 05 §14-15):
  * Case status never changes here directly: every move goes through `caregaps.service.transition` (the single
    lifecycle). Pass `user` from API routes (permission + assignment-scope enforced there) and only an `Actor` from
    trusted internal callers (the simulation job).
  * Nothing is ever delivered anywhere; "delivered" is a recorded state of a simulated event.
  * A message, a response or an appointment never completes a case; only closure with verified evidence does.
  * Functions add to the caller's transaction (flush, no commit).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from anticipa.caregaps import service as caregaps
from anticipa.caregaps.models import CASE_STATUSES, TERMINAL_STATUSES, CareGapCase
from anticipa.caregaps.service import (
    _check_scope as check_case_scope,  # one assignment-scope rule for every case action
)
from anticipa.closure.models import CompletionEvidence
from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.core.auth import CurrentUser
from anticipa.core.errors import Conflict, InvalidTransition, NotFound, ValidationFailed
from anticipa.datafabric.models import Resident
from anticipa.engagement.models import (
    APPOINTMENT_STATUSES,
    CHANNELS,
    DELIVERY_STATES,
    LANGUAGES,
    OPEN_APPOINTMENT_STATUSES,
    RESPONSE_TYPES,
    Appointment,
    CommunicationEvent,
    ResidentResponse,
)
from anticipa.engagement.templates import (
    DEFAULT_TEMPLATE_KEY,
    DIRECTION,
    ensure_templates,
    render,
    select_language,
)
from anticipa.planning.models import CapacitySlot, PlannedAllocation

OUTREACH_STATUSES = ("planned", "outreach_pending", "outreach_completed")  # a case must be planned before contact
PLANNABLE_STATUSES = ("approved_for_planning", "awaiting_capacity")  # a confirmed allocation moves these to planned
APPOINTMENT_CASE_STATUSES = (*PLANNABLE_STATUSES, *OUTREACH_STATUSES)
DELIVERY_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"sent", "delivered", "failed"}),
    "sent": frozenset({"delivered", "failed"}),
    "delivered": frozenset(),
    "failed": frozenset(),
}
APPOINTMENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"simulated_confirmed", "cancelled"}),
    "simulated_confirmed": frozenset({"completed", "no_show", "cancelled"}),
    "completed": frozenset(),
    "no_show": frozenset(),
    "cancelled": frozenset(),
}
QUESTION_ESCALATION_REASON = "Resident asked a question; clinician follow-up required (simulated response)"


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------------------------ drafts
def draft(session: Session, case: CareGapCase, *, template_key: str | None, language: str | None) -> dict[str, Any]:
    """Render the approved template for a case. Nothing is stored except the (idempotent) template rows."""
    key = template_key or DEFAULT_TEMPLATE_KEY
    resident = session.get(Resident, case.resident_id)
    lang, source = select_language(language, resident.preferred_language if resident else None)
    body = render(key, lang, case.care_activity)
    template = ensure_templates(session)[(key, lang)]
    return {
        "care_gap_case_id": case.id, "resident_id": case.resident_id, "care_activity": case.care_activity,
        "template_key": key, "template_version": template.version, "language": lang, "language_source": source,
        "direction": DIRECTION[lang], "body": body, "channels": list(CHANNELS), "constraints_checked": True,
        "simulated": True, "synthetic_status": "simulated",
        "notice": "Draft only: nothing is sent. Recording an outreach event simulates the send.",
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ communication events
def _stamp(event: CommunicationEvent, state: str, now: datetime) -> None:
    event.delivery_state = state
    if state in ("sent", "delivered", "failed") and event.sent_at is None:
        event.sent_at = now
    if state == "delivered":
        event.delivered_at = now


def _sync_case_for_delivery(session: Session, case: CareGapCase, event: CommunicationEvent, actor: Actor,
                            user: CurrentUser | None) -> None:  # fmt: skip
    """planned -> outreach_pending when outreach starts; outreach_pending -> outreach_completed once delivered."""
    details = {"communication_event_id": str(event.id), "simulated": True}
    if case.status == "planned":
        caregaps.transition(session, case, "outreach_pending", actor=actor, user=user,
                            reason="Simulated outreach recorded", details=details)  # fmt: skip
    if event.delivery_state == "delivered" and case.status == "outreach_pending":
        caregaps.transition(session, case, "outreach_completed", actor=actor, user=user,
                            reason="Simulated message delivered", details=details)  # fmt: skip


def record_event(
    session: Session, case: CareGapCase, *, channel: str, delivery_state: str = "queued", template_key: str | None = None,
    language: str | None = None, actor: Actor, user: CurrentUser | None = None, source: str = "manual",
    job_id: uuid.UUID | None = None, now: datetime | None = None,
) -> CommunicationEvent:  # fmt: skip
    """Record one SIMULATED outreach message for a planned case (server-rendered from an approved template)."""
    if channel not in CHANNELS or delivery_state not in DELIVERY_STATES:
        raise ValidationFailed("Invalid channel or delivery state.")
    case = caregaps.get_case(session, case.id, lock=True)
    check_case_scope(user, case)
    if case.status not in OUTREACH_STATUSES:
        raise InvalidTransition(
            f"Outreach cannot be recorded for a {case.status} case; it must be planned (capacity-confirmed) first.",
            [{"field": "status", "reason": f"allowed: {list(OUTREACH_STATUSES)}"}],
        )
    resident = session.get(Resident, case.resident_id)
    key = template_key or DEFAULT_TEMPLATE_KEY
    lang, _ = select_language(language, resident.preferred_language if resident else None)
    body = render(key, lang, case.care_activity)
    template = ensure_templates(session)[(key, lang)]
    now = now or _now()
    event = CommunicationEvent(
        id=uuid.uuid4(), care_gap_case_id=case.id, resident_id=case.resident_id, channel=channel, template_id=template.id,
        template_key=key, template_version=template.version, language=lang, body=body, source_type=source, job_id=job_id,
        created_by=actor.id, created_at=now, synthetic_status="simulated",
    )  # fmt: skip
    _stamp(event, delivery_state, now)
    session.add(event)
    session.flush()
    audit.record(session, actor, "engagement.event.record", "communication_event", event.id, "success", {
        "care_gap_case_id": str(case.id), "channel": channel, "delivery_state": delivery_state,
        "template": f"{key}@{template.version}", "language": lang, "source": source, "simulated": True,
    })  # fmt: skip
    _sync_case_for_delivery(session, case, event, actor, user)
    return event


def get_event(session: Session, event_id: uuid.UUID, *, lock: bool = False) -> CommunicationEvent:
    event = session.get(CommunicationEvent, event_id, with_for_update=lock, populate_existing=lock)
    if event is None:
        raise NotFound("Communication event not found.")
    return event


def set_delivery_state(session: Session, event: CommunicationEvent, new_state: str, *, actor: Actor,
                       user: CurrentUser | None = None, now: datetime | None = None) -> CommunicationEvent:  # fmt: skip
    event = get_event(session, event.id, lock=True)
    case = caregaps.get_case(session, event.care_gap_case_id, lock=True)
    check_case_scope(user, case)
    if new_state not in DELIVERY_TRANSITIONS[event.delivery_state]:
        raise InvalidTransition(
            f"A {event.delivery_state} message cannot become {new_state}.",
            [{"field": "delivery_state", "reason": f"allowed: {sorted(DELIVERY_TRANSITIONS[event.delivery_state]) or 'none'}"}],
        )  # fmt: skip
    previous = event.delivery_state
    _stamp(event, new_state, now or _now())
    session.flush()
    audit.record(session, actor, "engagement.event.delivery", "communication_event", event.id, "success",
                 {"from": previous, "to": new_state, "care_gap_case_id": str(case.id), "simulated": True})  # fmt: skip
    _sync_case_for_delivery(session, case, event, actor, user)
    return event


# ------------------------------------------------------------------------------------------------ resident responses
def record_response(
    session: Session, event: CommunicationEvent, *, response_type: str, response_value: str | None = None,
    actor: Actor, user: CurrentUser | None = None, source_reference: str | None = None, now: datetime | None = None,
) -> ResidentResponse:  # fmt: skip
    """Record a SIMULATED resident response to a delivered message. A `question` is out of scope for the system and is
    escalated to a clinician; `accept` / `decline` change nothing on the case by themselves (booking and closure are
    separate, audited steps)."""
    if response_type not in RESPONSE_TYPES:
        raise ValidationFailed("Invalid response type.")
    event = get_event(session, event.id, lock=True)
    case = caregaps.get_case(session, event.care_gap_case_id, lock=True)
    check_case_scope(user, case)
    if event.delivery_state != "delivered":
        raise Conflict("A response can only follow a delivered message.",
                       [{"field": "communication_event_id", "reason": f"message is {event.delivery_state}"}])  # fmt: skip
    now = now or _now()
    response = ResidentResponse(
        id=uuid.uuid4(), communication_event_id=event.id, care_gap_case_id=case.id, resident_id=case.resident_id,
        response_type=response_type, response_value=(response_value or None), received_at=now,
        source_reference=source_reference, created_by=actor.id, synthetic_status="simulated",
    )  # fmt: skip
    event.response_received_at = event.response_received_at or now
    session.add(response)
    session.flush()
    audit.record(session, actor, "engagement.response.record", "resident_response", response.id, "success", {
        "communication_event_id": str(event.id), "care_gap_case_id": str(case.id), "response_type": response_type,
        "simulated": True,
    })  # fmt: skip
    if response_type == "question" and case.status not in TERMINAL_STATUSES and case.status != "escalated":
        caregaps.escalate(session, case, actor=actor, user=user, reason=QUESTION_ESCALATION_REASON)
    return response


def communications_for_case(session: Session, case_id: uuid.UUID) -> list[tuple[CommunicationEvent, list[ResidentResponse]]]:
    events = session.execute(
        select(CommunicationEvent).where(CommunicationEvent.care_gap_case_id == case_id)
        .order_by(CommunicationEvent.created_at, CommunicationEvent.id)
    ).scalars().all()  # fmt: skip
    responses: dict[uuid.UUID, list[ResidentResponse]] = {e.id: [] for e in events}
    if events:
        for r in session.execute(
            select(ResidentResponse).where(ResidentResponse.communication_event_id.in_(list(responses)))
            .order_by(ResidentResponse.received_at, ResidentResponse.id)
        ).scalars():  # fmt: skip
            responses[r.communication_event_id].append(r)
    return [(e, responses[e.id]) for e in events]


# ------------------------------------------------------------------------------------------------ appointments
def get_appointment(session: Session, appointment_id: uuid.UUID, *, lock: bool = False) -> Appointment:
    appt = session.get(Appointment, appointment_id, with_for_update=lock, populate_existing=lock)
    if appt is None:
        raise NotFound("Appointment not found.")
    return appt


def _schedule_case(session: Session, case: CareGapCase, appointment_id: uuid.UUID, actor: Actor,
                   user: CurrentUser | None) -> None:  # fmt: skip
    caregaps.transition(session, case, "appointment_scheduled", actor=actor, user=user, reason="Simulated appointment confirmed",
                        details={"appointment_id": str(appointment_id), "simulated": True})  # fmt: skip


def create_appointment(
    session: Session, allocation_id: uuid.UUID, status: str = "proposed", *, actor: Actor,
    user: CurrentUser | None = None, source: str = "manual", job_id: uuid.UUID | None = None,
) -> Appointment:  # fmt: skip
    """Appointment record from a CONFIRMED allocation (planning only reserves the seat; this is where the booking
    record and the case's move to `planned` happen).

    `proposed` holds the slot for the resident and moves an approved case to `planned`; `simulated_confirmed` needs the
    outreach to have completed (the lifecycle refuses it otherwise) and moves the case to `appointment_scheduled`.
    A slot that has already started cannot be booked. Times and clinic come from the slot, never from the plan snapshot."""
    if status not in OPEN_APPOINTMENT_STATUSES:
        raise ValidationFailed("A new appointment is either proposed or simulated_confirmed.",
                               [{"field": "appointment_status", "reason": f"one of {list(OPEN_APPOINTMENT_STATUSES)}"}])  # fmt: skip
    alloc = session.get(PlannedAllocation, allocation_id, with_for_update=True)
    if alloc is None:
        raise NotFound("Planned allocation not found.")
    if alloc.allocation_status != "confirmed":
        raise Conflict("An appointment can only be created from a confirmed allocation; a plan is only a proposal.",
                       [{"field": "planned_allocation_id", "reason": f"allocation is {alloc.allocation_status}"}])  # fmt: skip
    slot = session.get(CapacitySlot, alloc.capacity_slot_id)
    if slot is None:
        raise Conflict("The allocated slot no longer exists.", [{"field": "capacity_slot_id", "reason": "missing"}])
    if slot.start_time <= _now():
        raise Conflict("The slot has already started; a past slot cannot be booked.",
                       [{"field": "capacity_slot_id", "reason": "slot start is in the past"}])  # fmt: skip
    case = caregaps.get_case(session, alloc.care_gap_case_id, lock=True)
    check_case_scope(user, case)
    if case.status in TERMINAL_STATUSES:
        raise Conflict(f"The case is {case.status}; reopen it first.")
    clash = session.execute(
        select(Appointment.id).where(
            (Appointment.planned_allocation_id == alloc.id)
            | ((Appointment.care_gap_case_id == case.id) & Appointment.appointment_status.in_(OPEN_APPOINTMENT_STATUSES))
        )
    ).first()
    if clash:
        raise Conflict("This case already has an appointment for this allocation or a live appointment.")
    if case.status not in APPOINTMENT_CASE_STATUSES:
        raise InvalidTransition(f"An appointment cannot be created for a {case.status} case.",
                                [{"field": "status", "reason": f"allowed: {list(APPOINTMENT_CASE_STATUSES)}"}])  # fmt: skip
    appt_id = uuid.uuid4()
    if status == "simulated_confirmed":  # lifecycle first: a refused move must leave no appointment row behind
        _schedule_case(session, case, appt_id, actor, user)
    elif case.status in PLANNABLE_STATUSES:
        caregaps.transition(session, case, "planned", actor=actor, user=user,
                            reason="Allocation confirmed; appointment proposed (simulated)",
                            details={"planned_allocation_id": str(alloc.id), "appointment_id": str(appt_id), "simulated": True})  # fmt: skip
    appt = Appointment(
        id=appt_id, care_gap_case_id=case.id, resident_id=case.resident_id, clinic_id=slot.clinic_id,
        clinic_service_id=slot.clinic_service_id, capacity_slot_id=slot.id, planned_allocation_id=alloc.id,
        start_time=slot.start_time, end_time=slot.end_time, appointment_status=status,
        source_type=source, job_id=job_id, created_by=actor.id, synthetic_status="simulated",
    )  # fmt: skip
    session.add(appt)
    try:
        session.flush()
    except IntegrityError as exc:  # lost a race with another appointment for the case / allocation
        raise Conflict("This case already has an appointment for this allocation or a live appointment.") from exc
    audit.record(session, actor, "appointment.create", "appointment", appt.id, "success", {
        "care_gap_case_id": str(case.id), "planned_allocation_id": str(alloc.id), "status": status, "source": source,
        "simulated": True,
    })  # fmt: skip
    return appt


def on_allocation_confirmed(session: Session, allocation_id: uuid.UUID, *, actor: Actor, user: CurrentUser | None = None,
                            source: str = "manual", job_id: uuid.UUID | None = None) -> tuple[Appointment, bool]:  # fmt: skip
    """Hook for a freshly CONFIRMED allocation (planning reserved the seat and did nothing else): hold the slot with a
    proposed, simulated appointment and move the case to `planned`. Idempotent: returns `(appointment, created)`."""
    existing = session.execute(select(Appointment).where(Appointment.planned_allocation_id == allocation_id)).scalar_one_or_none()
    if existing is not None:
        return existing, False
    return create_appointment(session, allocation_id, "proposed", actor=actor, user=user, source=source, job_id=job_id), True


def set_appointment_status(session: Session, appt: Appointment, new_status: str, *, reason: str | None = None,
                           actor: Actor, user: CurrentUser | None = None) -> Appointment:  # fmt: skip
    """Move an appointment along proposed -> simulated_confirmed -> completed | no_show | cancelled. Attending
    (`completed`) only moves the case to awaiting_completion_evidence; it never completes the case."""
    appt = get_appointment(session, appt.id, lock=True)
    case = caregaps.get_case(session, appt.care_gap_case_id, lock=True)
    check_case_scope(user, case)
    allowed = APPOINTMENT_TRANSITIONS[appt.appointment_status]
    if new_status not in allowed:
        raise InvalidTransition(f"A {appt.appointment_status} appointment cannot become {new_status}.",
                                [{"field": "appointment_status", "reason": f"allowed: {sorted(allowed) or 'none'}"}])  # fmt: skip
    previous = appt.appointment_status
    reason = (reason or "").strip() or None
    details: dict[str, Any] = {"from": previous, "to": new_status, "reason": reason, "care_gap_case_id": str(case.id), "simulated": True}
    if new_status == "simulated_confirmed":  # lifecycle first: a refused move must leave the appointment untouched
        _schedule_case(session, case, appt.id, actor, user)
    elif case.status == "appointment_scheduled":
        move = {"completed": ("awaiting_completion_evidence", "Appointment attended (simulated); completion evidence is required"),
                "no_show": ("outreach_pending", "Simulated no-show; outreach must be repeated"),
                "cancelled": ("outreach_pending", "Appointment cancelled; outreach must be repeated")}[new_status]  # fmt: skip
        caregaps.transition(session, case, move[0], actor=actor, user=user, reason=move[1],
                            details={"appointment_id": str(appt.id), "simulated": True})  # fmt: skip
    else:
        details["case_status_unchanged"] = case.status
    appt.appointment_status, appt.status_reason, appt.updated_at = new_status, reason, _now()
    session.flush()
    audit.record(session, actor, "appointment.status_change", "appointment", appt.id, "success", details)
    return appt


# ------------------------------------------------------------------------------------------------ overview
def _fill(counts: dict[Any, int], keys: Iterable[str]) -> dict[str, int]:
    return {**{k: 0 for k in keys}, **{str(k): int(v) for k, v in counts.items()}}


def _group(session: Session, column: Any) -> dict[Any, int]:
    return dict(session.execute(select(column, func.count()).group_by(column)).all())


def _cases_where(session: Session, column: Any, *where: Any) -> int:
    return session.execute(select(func.count(distinct(column))).where(*where)).scalar_one()


def engagement_overview(session: Session) -> dict[str, Any]:
    ce, rr, ap, ev = CommunicationEvent, ResidentResponse, Appointment, CompletionEvidence
    comm_total = session.execute(select(func.count()).select_from(ce)).scalar_one()
    resp_total = session.execute(select(func.count()).select_from(rr)).scalar_one()
    appt_total = session.execute(select(func.count()).select_from(ap)).scalar_one()
    evidence_total = session.execute(select(func.count()).select_from(ev)).scalar_one()
    return {
        "simulated": True,
        "synthetic_status": "simulated",
        "note": "All communications, responses, appointments and outcomes are simulated; no message was sent to a real person.",
        "communications": {
            "total": comm_total, "by_delivery_state": _fill(_group(session, ce.delivery_state), DELIVERY_STATES),
            "by_channel": _fill(_group(session, ce.channel), CHANNELS), "by_language": _fill(_group(session, ce.language), LANGUAGES),
        },
        "responses": {"total": resp_total, "by_type": _fill(_group(session, rr.response_type), RESPONSE_TYPES)},
        "appointments": {"total": appt_total, "by_status": _fill(_group(session, ap.appointment_status), APPOINTMENT_STATUSES)},
        "completion_evidence": {"total": evidence_total, "by_verification_status": _fill(_group(session, ev.verification_status), ("pending", "verified", "rejected"))},
        "cases": {"by_status": _fill(_group(session, CareGapCase.status), CASE_STATUSES)},
        "funnel": {
            "cases_contacted": _cases_where(session, ce.care_gap_case_id),
            "cases_delivered": _cases_where(session, ce.care_gap_case_id, ce.delivery_state == "delivered"),
            "cases_accepted": _cases_where(session, rr.care_gap_case_id, rr.response_type == "accept"),
            "cases_with_appointment": _cases_where(session, ap.care_gap_case_id, ap.appointment_status != "proposed"),
            "cases_attended": _cases_where(session, ap.care_gap_case_id, ap.appointment_status == "completed"),
            "cases_with_verified_evidence": _cases_where(session, ev.care_gap_case_id, ev.verification_status == "verified"),
            "cases_completed": session.execute(select(func.count()).select_from(CareGapCase).where(CareGapCase.status == "completed")).scalar_one(),
        },
    }  # fmt: skip
