"""Care-gap engine against the real database (integration, DB `anticipa_test`, rolled back): case creation
from evaluations
with duplicate prevention, validated transitions with history and audit, assignment scope, reopening."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from anticipa.caregaps import service
from anticipa.caregaps.models import CareGapCase, CaseStatusHistory
from anticipa.core.audit import Actor
from anticipa.core.auth import CurrentUser
from anticipa.core.errors import Conflict, Forbidden, InvalidTransition, ValidationFailed
from anticipa.guidelines.models import EligibilityEvaluation

ON = date(2026, 11, 8)
SYSTEM = Actor.system()


@pytest.fixture
def h(seed_module):
    return seed_module


def user(role: str) -> CurrentUser:
    return CurrentUser(
        id=uuid.uuid5(uuid.NAMESPACE_DNS, f"{role}.anticipa.test"), email=f"{role}@example.test", role=role
    )


def scan(h, db_session, version, **kw):
    return service.create_cases_scan(h.session_factory(db_session), [version.id], **kw)


def make_case(h, seed, db_session, **kw) -> CareGapCase:
    version, _ = seed.approved_version()
    resident = seed.resident(**kw)
    h.run_evaluation(db_session, [version], ON, resident_ids=[resident.id])
    assert scan(h, db_session, version)["created"] == 1
    return db_session.execute(select(CareGapCase).where(CareGapCase.resident_id == resident.id)).scalar_one()


def history(db_session, case):
    return [
        (x.from_status, x.to_status)
        for x in db_session.execute(
            select(CaseStatusHistory)
            .where(CaseStatusHistory.case_id == case.id)
            .order_by(CaseStatusHistory.occurred_at, CaseStatusHistory.id)
        ).scalars()
    ]


def walk(db_session, case, *statuses):
    for s in statuses:
        service.transition(db_session, case, s, actor=SYSTEM)


def review_rules(h):
    """Synthetic: an exclusion that routes to a clinician (requires_clinical_review) and an evidence-gap
    rule."""  # noqa: E501
    review = h.rule(
        "needs_clinician",
        "exclusion",
        30,
        h.leaf("evidence.selector", "EXISTS", "synth_excluding_condition"),
        {**h.TRUE_FAILS},
        h.when_true("requires_clinical_review"),
        "needs_clinician",
        review_class="individual_decision",
    )
    return [*h.demo_rules()[:3], review]


# ---------------------------------------------------------------------------------------------- creation
def test_eligible_residents_get_cases_that_are_auto_approved_for_planning_with_history(h, db_session, seed):
    version, _ = seed.approved_version()
    resident = seed.resident()
    young = seed.resident(birth=date(2005, 1, 1))
    done = seed.resident()
    seed.record(done, "procedure", date(2024, 1, 1))
    h.run_evaluation(db_session, [version], ON)
    result = scan(h, db_session, version)
    assert result["created"] == 1 and result["scanned"] == 1  # only the eligible resident qualifies
    case = db_session.execute(select(CareGapCase).where(CareGapCase.resident_id == resident.id)).scalar_one()
    assert (case.status, case.priority, case.source_outcome) == (
        "approved_for_planning",
        "routine",
        "eligible",
    )
    assert case.care_activity == version.care_activity and case.synthetic_status == "synthetic"
    assert case.rule_version_id is None  # an eligible resident has no decisive rule
    assert history(db_session, case) == [(None, "identified"), ("identified", "approved_for_planning")]
    evaluation = db_session.get(EligibilityEvaluation, case.evaluation_id)
    assert evaluation.resident_id == resident.id and evaluation.outcome == "eligible"
    assert (
        db_session.execute(
            select(CareGapCase).where(CareGapCase.resident_id.in_([young.id, done.id]))
        ).first()
        is None
    )


def test_manual_approval_mode_leaves_cases_identified(h, db_session, seed):
    version, _ = seed.approved_version()
    resident = seed.resident()
    h.run_evaluation(db_session, [version], ON)
    assert scan(h, db_session, version, auto_approve=False)["created"] == 1
    case = db_session.execute(select(CareGapCase).where(CareGapCase.resident_id == resident.id)).scalar_one()
    assert case.status == "identified" and history(db_session, case) == [(None, "identified")]


def test_only_clinician_review_outcomes_create_review_cases_never_missing_information_by_default(
    h, db_session, seed
):
    version, _ = seed.approved_version(review_rules(h))
    flagged = seed.resident()
    seed.fact(
        flagged,
        category="condition",
        fact_type="synthetic_condition",
        subject_type="resident",
        relationship=None,
        temporal="current",
    )
    seed.record(
        flagged, "condition", date(2020, 1, 1), c="COND-1"
    )  # COND-1 matches the exclusion selector by code
    uncovered = seed.resident(covered=False)  # insufficient evidence (no coverage)
    h.run_evaluation(db_session, [version], ON)
    outcomes = {e.resident_id: e.outcome for e in db_session.execute(select(EligibilityEvaluation)).scalars()}
    assert (
        outcomes[flagged.id] == "requires_clinical_review"
        and outcomes[uncovered.id] == "insufficient_evidence"
    )
    assert scan(h, db_session, version)["created"] == 1
    case = db_session.execute(select(CareGapCase).where(CareGapCase.resident_id == flagged.id)).scalar_one()
    assert (case.status, case.source_outcome) == (
        "pending_review",
        "requires_clinical_review",
    )  # a person decides; the planner never sees it
    assert history(db_session, case) == [(None, "identified"), ("identified", "pending_review")]
    opt_in = scan(
        h, db_session, version, outcomes=("eligible", "requires_clinical_review", "insufficient_evidence")
    )
    assert opt_in["created"] == 1  # the missing-information review queue is opt-in
    with pytest.raises(ValidationFailed):
        scan(h, db_session, version, outcomes=("not_eligible",))


def test_duplicate_open_cases_are_prevented_and_closed_cases_do_not_block_new_ones(h, db_session, seed):
    version, _ = seed.approved_version()
    resident = seed.resident()
    h.run_evaluation(db_session, [version], ON)
    assert scan(h, db_session, version)["created"] == 1
    assert scan(h, db_session, version)["created"] == 0  # idempotent: the evaluation already has its case
    h.run_evaluation(
        db_session, [version], ON, mode="full"
    )  # a re-evaluation: another eligible evaluation, same resident + activity
    again = scan(h, db_session, version)
    assert (again["created"], again["skipped_existing"]) == (
        0,
        1,
    )  # an open case already exists for resident + care activity
    (case,) = db_session.execute(select(CareGapCase).where(CareGapCase.resident_id == resident.id)).scalars()
    service.transition(db_session, case, "cancelled", actor=SYSTEM, reason="resident moved away")
    third = scan(h, db_session, version)
    assert third["created"] == 1  # the closed case no longer blocks the still-pending evaluation
    assert (
        len(
            db_session.execute(select(CareGapCase).where(CareGapCase.resident_id == resident.id))
            .scalars()
            .all()
        )
        == 2
    )
    # the database enforces it too, whatever the application does
    open_case = db_session.execute(
        select(CareGapCase).where(CareGapCase.resident_id == resident.id, CareGapCase.status != "cancelled")
    ).scalar_one()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                CareGapCase(
                    resident_id=resident.id,
                    evaluation_id=open_case.evaluation_id,
                    guideline_version_id=version.id,
                    care_activity=open_case.care_activity,
                    source_outcome="eligible",
                )
            )
            db_session.flush()


def test_a_latest_evaluation_that_is_no_longer_eligible_creates_no_case(h, db_session, seed):
    version, _ = seed.approved_version()
    resident = seed.resident()
    h.run_evaluation(db_session, [version], ON)
    seed.record(
        resident, "procedure", date(2025, 1, 1)
    )  # now up to date: the newest evaluation is already_completed
    h.run_evaluation(db_session, [version], ON)
    assert scan(h, db_session, version)["created"] == 0  # the older eligible evaluation is superseded


def reconcile(h, db_session, version, **kw):
    return service.reconcile_open_cases(h.session_factory(db_session), [version.id], **kw)


def test_a_newer_evaluation_cancels_an_unplanned_case_and_records_why(h, db_session, seed):
    version, _ = seed.approved_version()
    resident = seed.resident()
    h.run_evaluation(db_session, [version], ON, resident_ids=[resident.id])
    assert scan(h, db_session, version)["created"] == 1
    case = db_session.execute(select(CareGapCase).where(CareGapCase.resident_id == resident.id)).scalar_one()
    assert reconcile(h, db_session, version)["cancelled"] == 0  # nothing changed yet
    seed.record(resident, "procedure", date(2025, 1, 1))  # the screening is now documented
    h.run_evaluation(db_session, [version], ON, resident_ids=[resident.id])
    assert reconcile(h, db_session, version) == {
        "scanned": 1,
        "cancelled": 1,
        "returned_to_review": 0,
        "skipped": 0,
    }
    db_session.refresh(case)
    assert case.status == "cancelled" and case.closure_reason.endswith("already completed.")
    assert history(db_session, case)[-1] == ("approved_for_planning", "cancelled")
    last = (
        db_session.execute(
            select(CaseStatusHistory)
            .where(CaseStatusHistory.case_id == case.id)
            .order_by(CaseStatusHistory.occurred_at.desc())
        )
        .scalars()
        .first()
    )
    assert last.details["newest_outcome"] == "already_completed" and last.details["evaluation_id"]
    assert reconcile(h, db_session, version)["scanned"] == 0  # idempotent


def test_reconcile_leaves_planned_cases_alone_and_returns_a_review_case_for_a_clinician(h, db_session, seed):
    version, _ = seed.approved_version(review_rules(h))
    planned_resident, flagged = seed.resident(), seed.resident()
    h.run_evaluation(db_session, [version], ON)
    assert scan(h, db_session, version)["created"] == 2
    planned = db_session.execute(
        select(CareGapCase).where(CareGapCase.resident_id == planned_resident.id)
    ).scalar_one()
    walk(db_session, planned, "planned")
    seed.record(planned_resident, "procedure", date(2025, 1, 1))
    seed.fact(
        flagged,
        category="condition",
        fact_type="synthetic_condition",
        subject_type="resident",
        relationship=None,
        temporal="current",
    )
    seed.record(flagged, "condition", date(2020, 1, 1), c="COND-1")
    h.run_evaluation(db_session, [version], ON)
    result = reconcile(h, db_session, version)
    assert (result["cancelled"], result["returned_to_review"]) == (0, 1)  # the planned case is not touched
    db_session.refresh(planned)
    assert planned.status == "planned"
    case = db_session.execute(select(CareGapCase).where(CareGapCase.resident_id == flagged.id)).scalar_one()
    assert case.status == "pending_review"
    assert history(db_session, case)[-1] == ("approved_for_planning", "pending_review")


# ---------------------------------------------------------------------------------------------- transitions
def test_the_full_lifecycle_records_history_and_audit(h, db_session, seed):
    case = make_case(h, seed, db_session)
    walk(
        db_session,
        case,
        "awaiting_capacity",
        "planned",
        "outreach_pending",
        "outreach_completed",
        "appointment_scheduled",
        "awaiting_completion_evidence",
    )
    with pytest.raises(InvalidTransition, match="verified completion evidence"):
        service.transition(
            db_session, case, "completed", actor=SYSTEM
        )  # no shortcut through the generic transition
    done = service.complete_case(db_session, case, actor=SYSTEM, evidence_reference="completion_evidence:abc")
    assert done.status == "completed" and done.closed_at is not None and done.closure_reason
    states = [t for _, t in history(db_session, case)]
    assert states == [
        "identified",
        "approved_for_planning",
        "awaiting_capacity",
        "planned",
        "outreach_pending",
        "outreach_completed",
        "appointment_scheduled",
        "awaiting_completion_evidence",
        "completed",
    ]
    last = (
        db_session.execute(
            select(CaseStatusHistory)
            .where(CaseStatusHistory.case_id == case.id)
            .order_by(CaseStatusHistory.occurred_at.desc(), CaseStatusHistory.id.desc())
        )
        .scalars()
        .first()
    )
    assert last.details["completion_evidence"] == "completion_evidence:abc"
    audits = db_session.execute(
        text("SELECT count(*) FROM audit_events WHERE action = 'caregap.status_change' AND resource_id = :c"),
        {"c": str(case.id)},
    ).scalar_one()
    assert (
        audits == 7
    )  # one audit event per transition (the two creation rows are covered by the job's own summary event)
    with pytest.raises(ValidationFailed):
        service.complete_case(
            db_session, make_case(h, seed, db_session), actor=SYSTEM, evidence_reference=" "
        )


def test_invalid_transitions_raise_invalid_transition_and_change_nothing(h, db_session, seed):
    case = make_case(h, seed, db_session)  # approved_for_planning
    for bad in (
        "identified",
        "outreach_pending",
        "appointment_scheduled",
        "completed",
        "closed_not_completed",
        "approved_for_planning",
    ):
        with pytest.raises(InvalidTransition) as exc:
            service.transition(db_session, case, bad, actor=SYSTEM, reason="x")
        assert exc.value.code == "INVALID_TRANSITION" and exc.value.status_code == 409
    assert (
        db_session.get(CareGapCase, case.id).status == "approved_for_planning"
        and len(history(db_session, case)) == 2
    )


def test_reasons_are_required_where_it_matters_and_escalation_remembers_where_it_came_from(
    h, db_session, seed
):
    case = make_case(h, seed, db_session)
    with pytest.raises(ValidationFailed):
        service.escalate(db_session, case, actor=SYSTEM, user=None, reason="")
    service.escalate(db_session, case, actor=SYSTEM, user=None, reason="needs a doctor")
    last = (
        db_session.execute(
            select(CaseStatusHistory)
            .where(CaseStatusHistory.case_id == case.id)
            .order_by(CaseStatusHistory.occurred_at.desc(), CaseStatusHistory.id.desc())
        )
        .scalars()
        .first()
    )
    assert last.details["escalated_from"] == "approved_for_planning" and last.reason == "needs a doctor"
    service.transition(db_session, case, "pending_review", actor=SYSTEM)
    with pytest.raises(ValidationFailed):
        service.transition(db_session, case, "cancelled", actor=SYSTEM)


def test_reopen_and_the_one_open_case_rule(h, db_session, seed):
    case = make_case(h, seed, db_session)
    with pytest.raises(InvalidTransition):
        service.reopen(db_session, case, actor=SYSTEM, user=None, reason="still open")
    service.transition(db_session, case, "cancelled", actor=SYSTEM, reason="duplicate outreach")
    assert case.closed_at is not None and case.closure_reason == "duplicate outreach"
    with pytest.raises(ValidationFailed):
        service.reopen(db_session, case, actor=SYSTEM, user=None, reason=" ")
    service.reopen(db_session, case, actor=SYSTEM, user=None, reason="resident called back")
    assert case.status == "pending_review" and case.closed_at is None and case.closure_reason is None
    assert history(db_session, case)[-1] == ("cancelled", "pending_review")


def test_reopen_conflicts_with_an_existing_open_case(h, db_session, seed):
    version, _ = seed.approved_version()
    resident = seed.resident()
    h.run_evaluation(db_session, [version], ON)
    scan(h, db_session, version)
    (first,) = db_session.execute(select(CareGapCase).where(CareGapCase.resident_id == resident.id)).scalars()
    service.transition(db_session, first, "cancelled", actor=SYSTEM, reason="test")
    h.run_evaluation(db_session, [version], ON, mode="full")
    assert scan(h, db_session, version)["created"] == 1  # a fresh case for the same resident + activity
    with pytest.raises(Conflict):
        service.reopen(db_session, first, actor=SYSTEM, user=None, reason="try to reopen the old one")


def test_history_is_append_only_in_the_database(h, db_session, seed):
    case = make_case(h, seed, db_session)
    row = (
        db_session.execute(select(CaseStatusHistory).where(CaseStatusHistory.case_id == case.id))
        .scalars()
        .first()
    )
    with pytest.raises(DBAPIError, match="append-only"):
        with db_session.begin_nested():
            db_session.execute(
                text("UPDATE case_status_history SET reason = 'rewritten' WHERE id = :id"), {"id": row.id}
            )


# ---------------------------------------------------------------------------------------------- permissions
# and scope
def test_permissions_are_enforced_per_transition(h, db_session, seed):
    case = make_case(h, seed, db_session)  # approved_for_planning
    with pytest.raises(Forbidden):  # observers hold only *.view permissions
        service.transition(db_session, case, "awaiting_capacity", actor=SYSTEM, user=user("observer"))
    with pytest.raises(Forbidden):  # clinical reviewers do not run the planning pipeline
        service.transition(
            db_session, case, "awaiting_capacity", actor=SYSTEM, user=user("clinical_reviewer")
        )
    service.transition(db_session, case, "awaiting_capacity", actor=SYSTEM, user=user("care_coordinator"))
    service.transition(
        db_session, case, "pending_review", actor=SYSTEM, user=user("clinical_reviewer")
    )  # reviewers may send it to review...
    service.transition(
        db_session, case, "approved_for_planning", actor=SYSTEM, user=user("clinical_reviewer")
    )  # ...and back to planning
    service.transition(db_session, case, "planned", actor=SYSTEM, user=user("population_admin"))
    with pytest.raises(Forbidden):  # closing needs caregaps.close
        service.transition(db_session, case, "cancelled", actor=SYSTEM, user=user("clinical_reviewer"))
    service.transition(
        db_session, case, "cancelled", actor=SYSTEM, user=user("care_coordinator"), reason="duplicate"
    )


def test_coordinators_only_act_within_their_assigned_scope(h, db_session, seed):
    case = make_case(h, seed, db_session)
    mine, theirs = user("care_coordinator"), CurrentUser(id=uuid.uuid4(), email=None, role="care_coordinator")
    service.assign(db_session, case, mine.id, actor=mine.actor, user=mine)  # claiming an unassigned case
    with pytest.raises(Forbidden):  # another coordinator cannot touch it
        service.transition(db_session, case, "awaiting_capacity", actor=theirs.actor, user=theirs)
    with pytest.raises(Forbidden):
        service.assign(db_session, case, theirs.id, actor=theirs.actor, user=theirs)
    with pytest.raises(Forbidden):  # nor can a coordinator hand a case to someone else
        service.assign(db_session, case, uuid.uuid4(), actor=mine.actor, user=mine)
    service.transition(db_session, case, "awaiting_capacity", actor=mine.actor, user=mine)
    admin = user("population_admin")  # administrators are not limited to an assigned scope
    service.assign(db_session, case, theirs.id, actor=admin.actor, user=admin)
    assert case.assigned_coordinator_id == theirs.id
    service.assign(db_session, case, None, actor=admin.actor, user=admin)
    assert case.assigned_coordinator_id is None
    service.transition(db_session, case, "cancelled", actor=SYSTEM, reason="x")
    with pytest.raises(InvalidTransition):
        service.assign(
            db_session, case, mine.id, actor=admin.actor, user=admin
        )  # closed cases are not reassigned
