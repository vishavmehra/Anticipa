"""Loop closure policy (integration: DB `anticipa_test`, every test rolled back): a case completes ONLY with verified
completion evidence; outreach or an appointment alone never closes it; closing without completion is an authorised
exception with a type and a reason; evidence needs a second person's review."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from anticipa.caregaps import service as caregaps
from anticipa.caregaps.models import CaseStatusHistory
from anticipa.closure import service
from anticipa.core.audit import Actor
from anticipa.core.errors import Conflict, Forbidden, InvalidTransition, ValidationFailed
from anticipa.core.models import AuditEvent
from anticipa.engagement import service as engagement

REVIEWER = Actor(uuid.uuid5(uuid.NAMESPACE_DNS, "clinical_reviewer.anticipa.test"), "clinical_reviewer")
ATTESTATION = {"source_type": "manual_attestation", "evidence_reference": "clinic letter 2026-11-18 (synthetic)"}


def _verified(session, case, actor, reviewer=REVIEWER, **kw):
    evidence = service.submit_evidence(session, case, actor=actor, **{**ATTESTATION, **kw})
    return service.review_evidence(session, evidence, decision="verified", note="checked", actor=reviewer)


def test_completed_needs_verified_evidence_not_outreach_or_an_appointment(world, db_session, world_module):
    admin = world_module.ADMIN
    case, alloc = world.planned()
    engagement.record_event(db_session, case, channel="sms", delivery_state="delivered", actor=admin)
    for status in ("outreach_completed", "appointment_scheduled", "awaiting_completion_evidence"):
        db_session.refresh(case)
        if status == "appointment_scheduled":
            appt = engagement.create_appointment(db_session, alloc.id, "simulated_confirmed", actor=admin)
        if status == "awaiting_completion_evidence":
            engagement.set_appointment_status(db_session, appt, "completed", actor=admin)
        db_session.refresh(case)
        assert case.status == status
        with pytest.raises(ValidationFailed) as exc:  # no evidence at all
            service.close_case(db_session, case, outcome="completed", reason="Resident was contacted", actor=admin)
        assert "evidence" in exc.value.message.lower() and "appointment is not evidence" in exc.value.message
    assert case.status == "awaiting_completion_evidence"

    pending = service.submit_evidence(db_session, case, actor=admin, **ATTESTATION)
    assert pending.verification_status == "pending" and pending.verified_at is None and pending.synthetic_status == "simulated"
    with pytest.raises(ValidationFailed):  # pending evidence is not verified evidence
        service.close_case(db_session, case, outcome="completed", reason="Letter received", actor=admin)
    with pytest.raises(ValidationFailed):
        service.close_case(db_session, case, outcome="completed", reason="Letter received", evidence_id=pending.id, actor=admin)

    service.review_evidence(db_session, pending, decision="verified", note="matches the clinic record", actor=REVIEWER)
    result = service.close_case(db_session, case, outcome="completed", reason="Screening completed", actor=admin)
    db_session.refresh(case)
    assert case.status == "completed" and case.closed_at is not None and case.closure_reason == "Screening completed"
    assert result["status"] == "completed" and result["evidence_id"] == pending.id and result["simulated"] is True
    last = db_session.execute(
        select(CaseStatusHistory).where(CaseStatusHistory.case_id == case.id).order_by(CaseStatusHistory.occurred_at.desc()).limit(1)
    ).scalar_one()  # fmt: skip
    assert last.to_status == "completed" and last.details["completion_evidence"] == f"completion_evidence:{pending.id}"
    actions = {a for (a,) in db_session.execute(select(AuditEvent.action).where(AuditEvent.action.like("closure.%")))}
    assert {"closure.evidence_submit", "closure.evidence_review", "closure.close"} <= actions


def test_verified_evidence_cannot_skip_the_lifecycle(world, db_session, world_module):
    admin = world_module.ADMIN
    case = world.case("appointment_scheduled")  # appointment exists but the visit has not been recorded
    _verified(db_session, case, admin)
    with pytest.raises(InvalidTransition):
        service.close_case(db_session, case, outcome="completed", reason="Screening completed", actor=admin)
    db_session.refresh(case)
    assert case.status == "appointment_scheduled"
    with pytest.raises(InvalidTransition):  # the generic status API cannot complete a case either
        caregaps.transition(db_session, case, "completed", actor=admin)


def test_rejected_evidence_does_not_count_and_the_submitter_cannot_review_it(world, db_session, world_module):
    admin = world_module.ADMIN
    case = world.case("awaiting_completion_evidence")
    evidence = service.submit_evidence(db_session, case, actor=admin, **ATTESTATION)
    with pytest.raises(Forbidden):  # four eyes: not the person who submitted it
        service.review_evidence(db_session, evidence, decision="verified", note=None, actor=admin)
    with pytest.raises(ValidationFailed):  # a rejection needs a reason
        service.review_evidence(db_session, evidence, decision="rejected", note=None, actor=REVIEWER)
    service.review_evidence(db_session, evidence, decision="rejected", note="letter belongs to another resident", actor=REVIEWER)
    assert evidence.verification_status == "rejected" and evidence.verified_at is None
    with pytest.raises(InvalidTransition):  # decided once
        service.review_evidence(db_session, evidence, decision="verified", note=None, actor=REVIEWER)
    with pytest.raises(ValidationFailed):
        service.close_case(db_session, case, outcome="completed", reason="Screening completed", actor=admin)
    with pytest.raises(ValidationFailed):
        service.close_case(db_session, case, outcome="completed", reason="Screening completed", evidence_id=evidence.id, actor=admin)


def test_evidence_must_match_the_case_and_reference_real_records(world, db_session, world_module):
    admin = world_module.ADMIN
    case, other = world.case("awaiting_completion_evidence"), world.case("awaiting_completion_evidence")
    with pytest.raises(ValidationFailed):
        service.submit_evidence(db_session, case, actor=admin, activity="breast_cancer_screening", **ATTESTATION)
    with pytest.raises(ValidationFailed):
        service.submit_evidence(db_session, case, actor=admin, source_type="manual_attestation", evidence_reference="  ")
    with pytest.raises(ValidationFailed):  # a clinical-record reference must exist and belong to this resident
        service.submit_evidence(db_session, case, actor=admin, source_type="clinical_record", evidence_reference="record",
                                source_resource_id=str(uuid.uuid4()))  # fmt: skip
    foreign = world.record(other.resident_id)
    with pytest.raises(ValidationFailed):
        service.submit_evidence(db_session, case, actor=admin, source_type="clinical_record", evidence_reference="record",
                                source_resource_id=str(foreign.id))  # fmt: skip
    own = world.record(case.resident_id)
    ok = service.submit_evidence(db_session, case, actor=admin, source_type="clinical_record", evidence_reference="record",
                                 source_resource_id=str(own.id), activity=case.care_activity)  # fmt: skip
    assert ok.verification_status == "pending" and ok.activity == case.care_activity
    # evidence of one case cannot close another
    mine = _verified(db_session, case, admin)
    with pytest.raises(ValidationFailed):
        service.close_case(db_session, other, outcome="completed", reason="Screening completed", evidence_id=mine.id, actor=admin)


def test_only_the_simulation_gets_verified_on_arrival(world, db_session, world_module):
    case = world.case("awaiting_completion_evidence")
    kw = {"source_type": "simulated_procedure_record", "evidence_reference": "simulated-procedure-record:1"}
    human = service.submit_evidence(db_session, case, actor=world_module.ADMIN, **kw)
    system = service.submit_evidence(db_session, case, actor=world_module.SYSTEM, **kw)
    assert human.verification_status == "pending"
    assert system.verification_status == "verified" and system.verification_method == "simulated_record"
    assert system.synthetic_status == "simulated" and system.verified_at is not None


def test_database_requires_verified_at_exactly_when_verified(world, db_session, world_module):
    case = world.case("awaiting_completion_evidence")
    evidence = service.submit_evidence(db_session, case, actor=world_module.ADMIN, **ATTESTATION)
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.execute(text("UPDATE completion_evidence SET verification_status = 'verified' WHERE id = :i"), {"i": evidence.id})


def test_closing_without_completion_is_an_authorised_exception_with_a_type_and_reason(world, db_session, world_module):
    admin = world_module.ADMIN
    case = world.case("outreach_completed")
    with pytest.raises(ValidationFailed):
        service.close_case(db_session, case, outcome="closed_not_completed", reason="Resident declined", actor=admin)  # no type
    with pytest.raises(ValidationFailed):
        service.close_case(db_session, case, outcome="closed_not_completed", exception_type="resident_declined", reason=" ", actor=admin)
    with pytest.raises(ValidationFailed):  # an exception type has no place on a completion
        service.close_case(db_session, case, outcome="completed", exception_type="other", reason="Screening completed", actor=admin)
    result = service.close_case(db_session, case, outcome="closed_not_completed", exception_type="resident_declined",
                                reason="Declined during the call (simulated)", actor=admin)  # fmt: skip
    db_session.refresh(case)
    assert case.status == "closed_not_completed" and case.closure_reason.startswith("resident_declined:")
    assert result["exception_type"] == "resident_declined" and result["evidence_id"] is None
    with pytest.raises(InvalidTransition):  # already closed
        service.close_case(db_session, case, outcome="closed_not_completed", exception_type="other", reason="again", actor=admin)
    with pytest.raises(Conflict):  # no evidence on a closed case until it is reopened
        service.submit_evidence(db_session, case, actor=admin, **ATTESTATION)

    caregaps.reopen(db_session, case, actor=admin, user=None, reason="Resident changed their mind")
    assert case.status == "pending_review"


def test_planned_case_cannot_be_closed_without_completion(world, db_session, world_module):
    case = world.case("planned")
    with pytest.raises(InvalidTransition):  # lifecycle: cancel it instead
        service.close_case(db_session, case, outcome="closed_not_completed", exception_type="other", reason="not needed",
                           actor=world_module.ADMIN)  # fmt: skip
