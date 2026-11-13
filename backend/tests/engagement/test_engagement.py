"""Engagement: approved templates (en/hi/ar), language selection, simulated communication events, responses and
appointments (integration: DB `anticipa_test`, every test rolled back). Everything is labelled simulated."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from anticipa.caregaps.models import CaseStatusHistory
from anticipa.core.errors import Conflict, InvalidTransition, ValidationFailed
from anticipa.core.models import AuditEvent
from anticipa.engagement import service, templates
from anticipa.engagement.models import Appointment
from anticipa.engagement.simulate import SimRates, plan_outcome

ACTIVITY = "colorectal_cancer_screening"


def _has_script(text_: str, lo: str, hi: str) -> bool:
    return any(lo <= ch <= hi for ch in text_)


def _history(session, case) -> list[tuple[str | None, str]]:
    rows = session.execute(
        select(CaseStatusHistory).where(CaseStatusHistory.case_id == case.id).order_by(CaseStatusHistory.occurred_at)
    ).scalars()
    return [(h.from_status, h.to_status) for h in rows]


# ------------------------------------------------------------------------------------------ templates (no database)
def test_templates_render_in_three_languages_with_the_simulation_marker():
    bodies = {lang: templates.render("preventive_care_reminder", lang, ACTIVITY) for lang in ("en", "hi", "ar")}
    for lang, body in bodies.items():
        assert templates.SIMULATION_MARKERS[lang] in body  # "this is a simulated message" in the resident's language
        assert "{" not in body and "}" not in body
    assert "colorectal cancer screening" in bodies["en"]
    assert _has_script(bodies["hi"], "ऀ", "ॿ")  # Devanagari
    assert _has_script(bodies["ar"], "؀", "ۿ") and templates.DIRECTION["ar"] == "rtl"
    assert templates.activity_label(ACTIVITY, "ar") in bodies["ar"] and templates.activity_label(ACTIVITY, "hi") in bodies["hi"]
    assert templates.activity_label("some_new_screening", "hi") == "some new screening"  # unknown activity: English key


def test_message_constraints_reject_unmarked_or_clinical_claims():
    marker = templates.SIMULATION_MARKERS["en"]
    with pytest.raises(ValidationFailed):
        templates.check_constraints("Please book your screening.", "en")  # no simulated marker
    with pytest.raises(ValidationFailed):
        templates.check_constraints(f"{marker}\nYou have cancer.", "en")  # clinical claim
    with pytest.raises(ValidationFailed):
        templates.check_constraints(f"{marker}\nBook {{activity}}", "en")  # unfilled placeholder
    with pytest.raises(ValidationFailed):
        templates.render("no_such_template", "en", ACTIVITY)


def test_language_selection_prefers_request_then_resident_then_english():
    assert templates.select_language("ar", "hi") == ("ar", "requested")
    assert templates.select_language(None, "hi") == ("hi", "resident_preference")
    assert templates.select_language(None, "fr") == ("en", "default")
    assert templates.select_language(None, None) == ("en", "default")
    with pytest.raises(ValidationFailed):
        templates.select_language("fr", "en")


# ------------------------------------------------------------------------------------------ simulation plan (no database)
def test_plan_outcome_is_deterministic_by_seed_and_internally_consistent():
    ids = [uuid.uuid5(uuid.NAMESPACE_URL, f"case:{i}") for i in range(400)]
    first = [plan_outcome(7, i) for i in ids]
    assert first == [plan_outcome(7, i) for i in ids]  # same seed, same outcomes
    assert first != [plan_outcome(8, i) for i in ids]  # another seed, another world
    assert {o.final_status for o in first} >= {
        "completed", "closed_not_completed", "escalated", "outreach_completed", "outreach_pending", "awaiting_completion_evidence",
    }  # fmt: skip
    for o in first:
        assert o.delivered or o.response is None
        assert o.attendance is None or o.response == "accept"
        assert not o.evidence or o.attendance == "attended"
    with pytest.raises(ValueError):
        SimRates(accept=0.7, decline=0.3, question=0.1)


# ------------------------------------------------------------------------------------------ drafts and events
def test_draft_uses_the_residents_language_and_is_labelled_simulated(world, db_session):
    case, _ = world.planned("hi")
    draft = service.draft(db_session, case, template_key=None, language=None)
    assert (draft["language"], draft["language_source"], draft["direction"]) == ("hi", "resident_preference", "ltr")
    assert templates.SIMULATION_MARKERS["hi"] in draft["body"]
    assert draft["simulated"] is True and draft["synthetic_status"] == "simulated" and draft["constraints_checked"]
    arabic = service.draft(db_session, case, template_key=None, language="ar")
    assert (arabic["language"], arabic["language_source"], arabic["direction"]) == ("ar", "requested", "rtl")


def test_outreach_needs_a_planned_case(world, db_session, world_module):
    case = world.case("approved_for_planning")
    with pytest.raises(InvalidTransition):
        service.record_event(db_session, case, channel="sms", actor=world_module.ADMIN)


def test_event_is_simulated_audited_and_moves_the_case(world, db_session, world_module):
    case, _ = world.planned("ar")
    event = service.record_event(db_session, case, channel="sms", delivery_state="delivered", actor=world_module.ADMIN)
    assert event.synthetic_status == "simulated" and event.source_type == "manual"
    assert (event.language, event.template_key, event.template_version) == ("ar", "preventive_care_reminder", 1)
    assert templates.SIMULATION_MARKERS["ar"] in event.body
    assert event.sent_at is not None and event.delivered_at is not None
    db_session.refresh(case)
    assert case.status == "outreach_completed"
    assert _history(db_session, case)[-2:] == [("planned", "outreach_pending"), ("outreach_pending", "outreach_completed")]
    audit = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "engagement.event.record", AuditEvent.resource_id == str(event.id))
    ).scalar_one()  # fmt: skip
    assert audit.details["simulated"] is True and audit.details["delivery_state"] == "delivered"


def test_database_refuses_anything_but_simulated_engagement_rows(world, db_session, world_module):
    case, alloc = world.planned()
    event = service.record_event(db_session, case, channel="sms", delivery_state="delivered", actor=world_module.ADMIN)
    service.create_appointment(db_session, alloc.id, actor=world_module.ADMIN)
    for table in ("communication_events", "appointments"):
        with pytest.raises(DBAPIError), db_session.begin_nested():
            db_session.execute(text(f"UPDATE {table} SET synthetic_status = 'synthetic'"))  # noqa: S608
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.execute(text("UPDATE communication_events SET synthetic_status = 'real' WHERE id = :i"), {"i": event.id})


def test_delivery_state_machine_and_failed_delivery_keeps_case_in_outreach_pending(world, db_session, world_module):
    case, _ = world.planned()
    actor = world_module.ADMIN
    event = service.record_event(db_session, case, channel="whatsapp", actor=actor)  # queued
    assert event.delivery_state == "queued" and event.sent_at is None
    service.set_delivery_state(db_session, event, "failed", actor=actor)
    assert event.delivery_state == "failed" and event.sent_at is not None and event.delivered_at is None
    db_session.refresh(case)
    assert case.status == "outreach_pending"
    with pytest.raises(InvalidTransition):
        service.set_delivery_state(db_session, event, "delivered", actor=actor)


# ------------------------------------------------------------------------------------------ responses
def test_response_follows_delivery_and_a_question_is_escalated_to_a_clinician(world, db_session, world_module):
    case, _ = world.planned()
    actor = world_module.ADMIN
    event = service.record_event(db_session, case, channel="sms", actor=actor)  # queued
    with pytest.raises(Conflict):
        service.record_response(db_session, event, response_type="accept", actor=actor)  # not delivered yet
    service.set_delivery_state(db_session, event, "delivered", actor=actor)

    accept = service.record_response(db_session, event, response_type="accept", actor=actor)
    db_session.refresh(case)
    assert accept.synthetic_status == "simulated" and event.response_received_at is not None
    assert case.status == "outreach_completed"  # accepting books nothing and changes nothing by itself

    question = service.record_response(db_session, event, response_type="question", response_value="Is this safe for me?", actor=actor)
    db_session.refresh(case)
    assert case.status == "escalated" and question.synthetic_status == "simulated"
    assert "clinician" in _history_reason(db_session, case)
    audits = db_session.execute(select(AuditEvent).where(AuditEvent.action == "engagement.response.record")).scalars().all()
    assert all("Is this safe" not in str(a.details) for a in audits)  # free text never goes to the audit log


def _history_reason(session, case) -> str:
    return session.execute(
        select(CaseStatusHistory.reason).where(CaseStatusHistory.case_id == case.id, CaseStatusHistory.to_status == "escalated")
    ).scalar_one()


def test_communication_history_lists_events_with_responses(world, db_session, world_module):
    case, _ = world.planned()
    actor = world_module.ADMIN
    event = service.record_event(db_session, case, channel="sms", delivery_state="delivered", actor=actor)
    service.record_response(db_session, event, response_type="decline", actor=actor)
    (history,) = service.communications_for_case(db_session, case.id)
    assert history[0].id == event.id and [r.response_type for r in history[1]] == ["decline"]


# ------------------------------------------------------------------------------------------ appointments
def test_appointment_needs_a_confirmed_allocation(world, db_session, world_module):
    case = world.case("outreach_completed")
    proposal = world.allocation(case, confirm=False)
    with pytest.raises(Conflict):
        service.create_appointment(db_session, proposal.id, actor=world_module.ADMIN)  # a plan is not a booking
    assert db_session.execute(select(Appointment)).first() is None


def test_simulated_confirmation_needs_completed_outreach_and_leaves_no_row_when_refused(world, db_session, world_module):
    case, alloc = world.planned()  # planned, never contacted
    with pytest.raises(InvalidTransition):
        service.create_appointment(db_session, alloc.id, "simulated_confirmed", actor=world_module.ADMIN)
    assert db_session.execute(select(Appointment).where(Appointment.care_gap_case_id == case.id)).first() is None
    proposed = service.create_appointment(db_session, alloc.id, "proposed", actor=world_module.ADMIN)  # allowed; case unchanged
    db_session.refresh(case)
    assert proposed.appointment_status == "proposed" and case.status == "planned"


def test_confirmed_allocation_moves_the_case_to_planned_and_holds_a_proposed_appointment(world, db_session, world_module):
    case = world.case("approved_for_planning")
    alloc = world.allocation(case)  # planning confirmed it: seat reserved, nothing else happened
    assert case.status == "approved_for_planning"
    appt, created = service.on_allocation_confirmed(db_session, alloc.id, actor=world_module.ADMIN)
    db_session.refresh(case)
    assert created and appt.appointment_status == "proposed" and appt.synthetic_status == "simulated"
    assert (appt.capacity_slot_id, appt.start_time) == (alloc.capacity_slot_id, alloc.proposed_start_time)
    assert case.status == "planned" and _history(db_session, case)[-1] == ("approved_for_planning", "planned")
    again, created = service.on_allocation_confirmed(db_session, alloc.id, actor=world_module.ADMIN)  # idempotent
    assert again.id == appt.id and not created


def test_planning_confirm_holds_the_slot_through_the_engagement_hook(world, db_session, world_module):
    from anticipa.planning import service as planning

    case = world.case("approved_for_planning")
    alloc = world.allocation(case, confirm=False)
    result = planning.confirm_allocation(db_session, alloc.id, world_module.ADMIN)
    db_session.refresh(case)
    assert result.newly_confirmed and result.appointment_created
    (appt,) = db_session.execute(select(Appointment).where(Appointment.care_gap_case_id == case.id)).scalars().all()
    assert appt.appointment_status == "proposed" and appt.planned_allocation_id == alloc.id and appt.synthetic_status == "simulated"
    assert case.status == "planned"
    with pytest.raises(Conflict):  # a second manual hold is a duplicate; the hook itself is idempotent
        service.create_appointment(db_session, alloc.id, "proposed", actor=world_module.ADMIN)
    assert service.on_allocation_confirmed(db_session, alloc.id, actor=world_module.ADMIN) == (appt, False)


def test_a_slot_that_has_started_cannot_be_booked(world, db_session, world_module):
    case = world.case("approved_for_planning")
    alloc = world.allocation(case, past=True)
    for status in ("proposed", "simulated_confirmed"):
        with pytest.raises(Conflict) as exc:
            service.create_appointment(db_session, alloc.id, status, actor=world_module.ADMIN)
        assert "past" in exc.value.message
    db_session.refresh(case)
    assert case.status == "approved_for_planning" and db_session.execute(select(Appointment)).first() is None


@pytest.mark.parametrize("status", ["identified", "pending_review", "escalated", "awaiting_completion_evidence"])
def test_appointment_needs_a_plannable_or_contacted_case(world, db_session, world_module, status):
    case = world.case(status)
    alloc = world.allocation(case)
    with pytest.raises(InvalidTransition):
        service.create_appointment(db_session, alloc.id, "proposed", actor=world_module.ADMIN)


def test_appointment_lifecycle_never_completes_the_case(world, db_session, world_module):
    case, alloc = world.planned()
    actor = world_module.ADMIN
    service.record_event(db_session, case, channel="sms", delivery_state="delivered", actor=actor)
    appt = service.create_appointment(db_session, alloc.id, "simulated_confirmed", actor=actor)
    db_session.refresh(case)
    assert appt.synthetic_status == "simulated" and appt.start_time == alloc.proposed_start_time
    assert case.status == "appointment_scheduled"
    with pytest.raises(Conflict):  # one live appointment per case
        service.create_appointment(db_session, alloc.id, actor=actor)

    service.set_appointment_status(db_session, appt, "completed", actor=actor)
    db_session.refresh(case)
    assert appt.appointment_status == "completed"
    assert case.status == "awaiting_completion_evidence"  # attending is not completion
    with pytest.raises(InvalidTransition):
        service.set_appointment_status(db_session, appt, "no_show", actor=actor)


@pytest.mark.parametrize("outcome", ["no_show", "cancelled"])
def test_missed_or_cancelled_appointment_sends_the_case_back_to_outreach(world, db_session, world_module, outcome):
    case, alloc = world.planned()
    actor = world_module.ADMIN
    service.record_event(db_session, case, channel="sms", delivery_state="delivered", actor=actor)
    appt = service.create_appointment(db_session, alloc.id, "simulated_confirmed", actor=actor)
    service.set_appointment_status(db_session, appt, outcome, reason="test", actor=actor)
    db_session.refresh(case)
    assert appt.appointment_status == outcome and case.status == "outreach_pending"


def test_engagement_overview_counts_by_state_and_is_labelled_simulated(world, db_session, world_module):
    case, alloc = world.planned()
    actor = world_module.ADMIN
    event = service.record_event(db_session, case, channel="sms", delivery_state="delivered", actor=actor)
    service.record_response(db_session, event, response_type="accept", actor=actor)
    service.create_appointment(db_session, alloc.id, "simulated_confirmed", actor=actor)
    overview = service.engagement_overview(db_session)
    assert overview["simulated"] is True and overview["synthetic_status"] == "simulated"
    assert overview["communications"]["by_delivery_state"]["delivered"] >= 1
    assert overview["responses"]["by_type"]["accept"] >= 1
    assert overview["appointments"]["by_status"]["simulated_confirmed"] >= 1
    assert overview["funnel"]["cases_contacted"] >= 1 and overview["cases"]["by_status"]["appointment_scheduled"] >= 1
