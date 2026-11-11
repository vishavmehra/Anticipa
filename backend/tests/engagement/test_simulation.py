"""The `engagement.simulate` job: outcomes follow the seed-deterministic plan, every row is simulated, no case is ever
completed without verified evidence, and a re-run changes nothing (integration: DB `anticipa_test`, rolled back)."""

from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy import func, select

from anticipa.caregaps.models import CareGapCase
from anticipa.closure.models import CompletionEvidence
from anticipa.core.models import AuditEvent
from anticipa.engagement.handlers import handle
from anticipa.engagement.models import Appointment, CommunicationEvent, ResidentResponse
from anticipa.engagement.simulate import SimScope, plan_outcome, select_cohort, simulate_case
from anticipa.jobs import service as jobs
from anticipa.jobs.context import JobContext
from anticipa.jobs.models import ProcessingJob
from anticipa.planning.models import CapacitySlot

N = 90


def _world_cases(world, n: int = N):
    """Half with a confirmed allocation, half only proposed; a mix of `planned` and `approved_for_planning`."""
    out = []
    for i in range(n):
        case = world.case("planned" if i % 2 == 0 else "approved_for_planning", language=("en", "hi", "ar")[i % 3])
        out.append((case, world.allocation(case, confirm=i % 4 < 2)))
    return out


def test_cohort_is_ordered_limited_and_skips_cases_without_an_allocation(world, db_session):
    cases = _world_cases(world, 6)
    world.case("planned")  # no allocation at all: not in the cohort
    scope = SimScope(seed=1, planning_run_id=world.run.id)
    cohort = select_cohort(db_session, scope)
    assert [c for c, _ in cohort] == sorted((c.id for c, _ in cases), key=str)
    assert len(select_cohort(db_session, SimScope(seed=1, limit=2))) == 2
    # without confirm_proposed only the confirmed allocations qualify
    confirmed_only = select_cohort(db_session, SimScope(seed=1, confirm_proposed=False))
    assert {c for c, _ in confirmed_only} == {c.id for c, a in cases if a.allocation_status == "confirmed"}


def test_simulation_follows_the_deterministic_plan_and_labels_everything_simulated(world, db_session, world_module):
    cases = _world_cases(world)
    scope = SimScope(seed=11, planning_run_id=world.run.id)
    results = {}
    for case_id, alloc_id in select_cohort(db_session, scope):
        results[case_id] = simulate_case(db_session, case_id, alloc_id, scope, world_module.SYSTEM)

    assert all(r is not None for r in results.values())
    finals = Counter()
    for case, _ in cases:
        db_session.refresh(case)
        expected = plan_outcome(11, case.id, scope.rates)
        assert results[case.id] == expected  # the run IS the plan
        assert case.status == expected.final_status
        finals[case.status] += 1
    assert len(finals) >= 4 and finals["completed"] > 0 and finals["closed_not_completed"] > 0

    ids = [c.id for c, _ in cases]
    for model in (CommunicationEvent, ResidentResponse, Appointment):
        rows = db_session.execute(select(model).where(model.care_gap_case_id.in_(ids))).scalars().all()
        assert rows and {r.synthetic_status for r in rows} == {"simulated"}
    assert {e.source_type for e in db_session.execute(select(CommunicationEvent).where(CommunicationEvent.care_gap_case_id.in_(ids))).scalars()} == {"simulation"}

    # loop closure: completed <=> verified evidence of the case, and the appointment was attended
    completed = [c.id for c, _ in cases if c.status == "completed"]
    evidenced = set(db_session.execute(
        select(CompletionEvidence.care_gap_case_id).where(
            CompletionEvidence.care_gap_case_id.in_(ids), CompletionEvidence.verification_status == "verified",
            CompletionEvidence.source_type == "simulated_procedure_record", CompletionEvidence.synthetic_status == "simulated",
        )
    ).scalars())  # fmt: skip
    assert set(completed) == evidenced
    attended = set(db_session.execute(
        select(Appointment.care_gap_case_id).where(Appointment.care_gap_case_id.in_(ids), Appointment.appointment_status == "completed")
    ).scalars())  # fmt: skip
    assert set(completed) <= attended
    # attended but without a completion record stays open: an appointment alone never closes a case
    stuck = [c for c, _ in cases if c.status == "awaiting_completion_evidence"]
    assert stuck and all(c.id in attended and c.id not in evidenced for c in stuck)
    # declines closed through the exception path, with the reason recorded
    declined = [c for c, _ in cases if c.status == "closed_not_completed"]
    assert all(c.closure_reason.startswith("resident_declined") for c in declined)

    audits = Counter(db_session.execute(select(AuditEvent.action).where(AuditEvent.action.like("engagement.%") | AuditEvent.action.like("appointment.%") | AuditEvent.action.like("closure.%"))).scalars())
    assert audits["engagement.event.record"] >= len(cases) and audits["closure.close"] >= len(completed)


def test_job_handler_runs_the_cohort_once_and_a_rerun_changes_nothing(world, db_session):
    cases = _world_cases(world, 30)
    sessions = world.sessions()
    scope = SimScope(seed=5, planning_run_id=world.run.id).model_dump(mode="json")
    with sessions() as s:
        job = jobs.submit_job(s, "engagement.simulate", scope, None, None).job
        job.status, job.locked_by = "running", "test-worker"
        s.commit()

    handle(JobContext(sessions, job, "test-worker"))
    stored = db_session.get(ProcessingJob, job.id, populate_existing=True)
    assert (stored.total_items, stored.processed_items, stored.failed_items) == (30, 30, 0)
    assert stored.checkpoint["stage"] == "done" and sum(stored.checkpoint["results"].values()) == 30
    for case, _ in cases:
        db_session.refresh(case)
        assert case.status == plan_outcome(5, case.id).final_status

    def counts():
        return tuple(db_session.execute(select(func.count()).select_from(m)).scalar_one() for m in (CommunicationEvent, ResidentResponse, Appointment, CompletionEvidence))

    after_first = counts()
    again = jobs.submit_job(db_session, "engagement.simulate", scope, None, None).job
    again.status, again.locked_by = "running", "test-worker"
    db_session.flush()
    handle(JobContext(sessions, again, "test-worker"))
    assert counts() == after_first  # handled cases are no longer waiting for contact: nothing is duplicated
    assert db_session.execute(select(func.count()).select_from(CareGapCase).where(CareGapCase.status == "planned")).scalar_one() == 0


def test_a_slot_without_capacity_skips_the_case_instead_of_failing(world, db_session, world_module):
    case = world.case("approved_for_planning")
    alloc = world.allocation(case, confirm=False, seats=1)
    slot = db_session.get(CapacitySlot, alloc.capacity_slot_id)
    slot.reserved_capacity = slot.available_capacity  # simulated slot filled in the meantime
    db_session.flush()
    scope = SimScope(seed=1, planning_run_id=world.run.id)
    assert simulate_case(db_session, case.id, alloc.id, scope, world_module.SYSTEM) is None
    db_session.refresh(case)
    assert case.status == "approved_for_planning"  # untouched; it waits for the next plan


def test_invalid_scope_is_rejected():
    with pytest.raises(ValueError):
        SimScope.model_validate({"seed": 1, "rates": {"accept": 0.9, "decline": 0.5}})
    with pytest.raises(ValueError):
        SimScope.model_validate({"seed": 1, "unknown": True})
