"""Deterministic engagement simulation (job `engagement.simulate`).

For each planned case it plays the whole loop with SIMULATED outcomes, every step through the same services the API
uses (so lifecycle rules, audit events and the `simulated` labels cannot be bypassed):

    confirm allocation -> simulated message -> delivery outcome -> resident response -> appointment -> attendance
        -> completion evidence (simulated procedure record) -> verified closure

The outcome of a case is a pure function of (seed, case id, rates, the resident's consent): re-running with
the same seed reproduces the same outcomes, and cases are processed in id order. A resident who opted out of
the drawn channel is contacted on the first permitted one; one who opted out of every channel is skipped
(ADR-070). Nothing is ever sent anywhere.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from anticipa.caregaps import service as caregaps
from anticipa.caregaps.models import CareGapCase
from anticipa.closure import service as closure
from anticipa.consent import service as consent
from anticipa.core.audit import Actor
from anticipa.core.errors import Conflict
from anticipa.engagement import service as engagement
from anticipa.engagement.models import Appointment
from anticipa.planning import service as planning
from anticipa.planning.models import CapacitySlot, PlannedAllocation, PlanningRun

SIM_CHANNELS = ("sms", "whatsapp", "phone_call")
COHORT_STATUSES = ("planned", "approved_for_planning", "awaiting_capacity")
SIMULATED_QUESTION = "(simulated) Resident asked a question about their health."


class SimRates(BaseModel):
    """Probabilities of the simulated world. The remainder of each group is the last outcome (no response / cancelled)."""

    model_config = ConfigDict(extra="forbid")
    delivery_failure: float = Field(0.06, ge=0, le=1)
    accept: float = Field(0.62, ge=0, le=1)
    decline: float = Field(0.10, ge=0, le=1)
    question: float = Field(0.06, ge=0, le=1)
    attend: float = Field(0.78, ge=0, le=1)
    no_show: float = Field(0.14, ge=0, le=1)
    evidence: float = Field(0.85, ge=0, le=1)  # an attended appointment yields a completion record

    @model_validator(mode="after")
    def _groups(self) -> Self:
        if self.accept + self.decline + self.question > 1 or self.attend + self.no_show > 1:
            raise ValueError("response probabilities (accept+decline+question) and attendance (attend+no_show) must each sum to <= 1")
        return self


class SimScope(BaseModel):
    """`engagement.simulate` input_scope."""

    model_config = ConfigDict(extra="forbid")
    seed: int = 1
    limit: int | None = Field(500, ge=1, le=100_000)
    planning_run_id: uuid.UUID | None = None  # default: the latest completed run
    confirm_proposed: bool = True  # confirm the run's proposed allocations first (reserves simulated seats)
    case_ids: list[uuid.UUID] | None = Field(None, max_length=100_000)
    rates: SimRates = Field(default_factory=SimRates)


@dataclass(frozen=True)
class Outcome:
    channel: str
    delivered: bool
    response: str | None  # accept | decline | question | no_response (None when the message was not delivered)
    attendance: str | None  # attended | no_show | cancelled (only after accept)
    evidence: bool  # a completion record exists (only after attended)

    @property
    def final_status(self) -> str:
        """The case status this outcome ends in."""
        if not self.delivered:
            return "outreach_pending"
        if self.response == "no_response":
            return "outreach_completed"
        if self.response == "question":
            return "escalated"
        if self.response == "decline":
            return "closed_not_completed"
        if self.attendance in ("no_show", "cancelled"):
            return "outreach_pending"
        return "completed" if self.evidence else "awaiting_completion_evidence"


def plan_outcome(seed: int, case_id: uuid.UUID, rates: SimRates | None = None) -> Outcome:
    """Pure and stable: `random.Random(str)` seeds from a SHA-512 of the string, independent of PYTHONHASHSEED."""
    r = rates or SimRates()
    rng = random.Random(f"anticipa.engagement:{seed}:{case_id}")  # noqa: S311  (simulation, not security)
    u_channel, u_delivery, u_response, u_attend, u_evidence = (rng.random() for _ in range(5))  # fixed draw order
    channel = SIM_CHANNELS[int(u_channel * len(SIM_CHANNELS))]
    if u_delivery < r.delivery_failure:
        return Outcome(channel, False, None, None, False)
    if u_response < r.accept:
        response = "accept"
    elif u_response < r.accept + r.decline:
        response = "decline"
    elif u_response < r.accept + r.decline + r.question:
        response = "question"
    else:
        response = "no_response"
    if response != "accept":
        return Outcome(channel, True, response, None, False)
    if u_attend < r.attend:
        return Outcome(channel, True, response, "attended", u_evidence < r.evidence)
    return Outcome(channel, True, response, "no_show" if u_attend < r.attend + r.no_show else "cancelled", False)


def select_cohort(session: Session, scope: SimScope) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(case_id, allocation_id), ordered by case id: cases with a confirmed allocation, plus (confirm_proposed) the
    run's proposed ones; only cases still waiting for contact and without any appointment yet."""
    pa, c = PlannedAllocation, CareGapCase
    base = (
        select(pa.care_gap_case_id, pa.id).join(c, c.id == pa.care_gap_case_id)
        .where(c.status.in_(COHORT_STATUSES), ~exists().where(Appointment.care_gap_case_id == c.id))
    )  # fmt: skip
    if scope.case_ids:
        base = base.where(c.id.in_(scope.case_ids))
    chosen = dict(session.execute(base.where(pa.allocation_status == "confirmed")).all())
    if scope.confirm_proposed:
        run_id = scope.planning_run_id or session.execute(
            select(PlanningRun.id).where(PlanningRun.status == "completed").order_by(PlanningRun.completed_at.desc()).limit(1)
        ).scalar_one_or_none()  # fmt: skip
        if run_id is not None:
            for case_id, alloc_id in session.execute(base.where(pa.allocation_status == "proposed", pa.planning_run_id == run_id)).all():
                chosen.setdefault(case_id, alloc_id)
    cohort = sorted(chosen.items(), key=lambda kv: str(kv[0]))
    return cohort[: scope.limit] if scope.limit else cohort


def simulate_case(session: Session, case_id: uuid.UUID, allocation_id: uuid.UUID, scope: SimScope, actor: Actor,
                  job_id: uuid.UUID | None = None) -> Outcome | None:  # fmt: skip
    """One case, one transaction (the caller commits). None = skipped (not eligible any more / no capacity / past slot)."""
    case = caregaps.get_case(session, case_id, lock=True)
    if case.status not in COHORT_STATUSES:
        return None
    alloc = session.get(PlannedAllocation, allocation_id, with_for_update=True)
    if alloc is None or alloc.care_gap_case_id != case.id:
        return None
    slot = session.get(CapacitySlot, alloc.capacity_slot_id)
    if slot is None or slot.start_time <= datetime.now(UTC):
        return None  # a slot that has already started cannot be confirmed or booked
    state = consent.state_for(session, case.resident_id)  # ADR-070
    channels = [c for c in SIM_CHANNELS if c in state.permitted_channels]
    if not channels:  # opted out of every channel the simulation uses: no seat reserved, no message simulated
        consent.audit_outreach_blocked(session, actor, case.resident_id, None, case_id=case.id,
                                       stage="simulation", job_id=str(job_id) if job_id else None)
        return None
    if alloc.allocation_status == "proposed":
        if not scope.confirm_proposed:
            return None
        try:  # reserves the seat and (same transaction) holds the slot via on_allocation_confirmed
            planning.confirm_allocation(session, alloc.id, actor, source="simulation", job_id=job_id)
        except Conflict:  # slot full / closed / hold refused: planning audits it; this case waits for the next plan
            return None
    elif alloc.allocation_status != "confirmed":
        return None
    source = {"source": "simulation", "job_id": job_id}
    # idempotent: planning's confirm already held the slot; this covers allocations that were confirmed without a hold
    appt, _ = engagement.on_allocation_confirmed(session, alloc.id, actor=actor, **source)

    outcome = plan_outcome(scope.seed, case.id, scope.rates)
    if outcome.channel not in channels:  # the drawn channel is opted out: use the first permitted one
        outcome = replace(outcome, channel=channels[0])
    event = engagement.record_event(
        session, case, channel=outcome.channel, delivery_state="delivered" if outcome.delivered else "failed",
        actor=actor, **source,
    )  # fmt: skip
    if not outcome.delivered:
        return outcome  # case stays outreach_pending with the held (proposed) appointment; a coordinator can retry
    engagement.record_response(
        session, event, response_type=outcome.response or "no_response", actor=actor,
        response_value=SIMULATED_QUESTION if outcome.response == "question" else None,
        source_reference=f"simulation:seed={scope.seed}",
    )  # fmt: skip
    if outcome.response == "decline":
        engagement.set_appointment_status(session, appt, "cancelled", reason="Resident declined (simulated response)", actor=actor)
        closure.close_case(session, case, outcome="closed_not_completed", exception_type="resident_declined", actor=actor,
                           reason="Resident declined (simulated response)")  # fmt: skip
    elif outcome.response == "accept":
        engagement.set_appointment_status(session, appt, "simulated_confirmed", reason="Resident accepted (simulated)", actor=actor)
        status = "completed" if outcome.attendance == "attended" else outcome.attendance
        engagement.set_appointment_status(session, appt, status or "completed", actor=actor,
                                          reason=f"Simulated attendance outcome: {outcome.attendance}")  # fmt: skip
        if outcome.evidence:
            evidence = closure.submit_evidence(
                session, case, source_type="simulated_procedure_record", source_resource_type="Procedure",
                source_resource_id=f"simulated:{scope.seed}:{case.id}",
                evidence_reference=f"simulated-procedure-record:{scope.seed}:{case.id}", actor=actor,
            )  # fmt: skip
            closure.close_case(session, case, outcome="completed", evidence_id=evidence.id, actor=actor,
                               reason="Verified completion evidence (simulated procedure record)")  # fmt: skip
    return outcome
