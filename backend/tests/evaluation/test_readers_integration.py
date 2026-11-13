"""The read-only result readers against real rows (anticipa_test, rolled back): the SQL, the row shapes and the metrics that
consume them, for eligibility, planning, closure, engagement, cost and jobs. Seeds one small connected pipeline result."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from anticipa.ai.models import ModelInvocation
from anticipa.caregaps.models import CareGapCase, CaseStatusHistory
from anticipa.closure.models import CompletionEvidence
from anticipa.datafabric.models import Dataset, Resident
from anticipa.engagement.models import Appointment, CommunicationEvent
from anticipa.evaluation import closure_eval, eligibility, perf, planning_eval, readers
from anticipa.guidelines.models import EligibilityEvaluation, Guideline, GuidelineVersion
from anticipa.jobs.models import ProcessingJob
from anticipa.planning.domain import REASON_CODES
from anticipa.planning.models import (
    CapacitySlot,
    Clinic,
    ClinicService,
    PlannedAllocation,
    PlanningRun,
    UnallocatedCase,
)

pytestmark = pytest.mark.integration
T0 = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def seeded(db_session):
    s = db_session
    ds = Dataset(dataset_version=f"syn-test-{uuid.uuid4().hex[:8]}", generator="test")
    s.add(ds)
    s.flush()
    res = []
    for lang, district in (("en", "D1"), ("hi", "D2"), ("ar", "D1")):
        r = Resident(dataset_id=ds.id, source_system="test", source_patient_id=uuid.uuid4().hex, birth_date=date(1974, 1, 1), administrative_sex="male",
                     preferred_language=lang, district_code=district)  # fmt: skip
        s.add(r)
        res.append(r)
    s.flush()
    g = Guideline(guideline_key=f"colorectal_average_risk-{uuid.uuid4().hex[:6]}", name="crc")
    s.add(g)
    s.flush()
    gv = GuidelineVersion(guideline_id=g.id, version=1, effective_from=date(2026, 1, 1), care_activity="colorectal_screening")
    s.add(gv)
    s.flush()
    evs = []
    for r, (outcome, cls) in zip(res, (("already_completed", "ineligible"), ("eligible", "eligible"), ("eligible", "eligible")), strict=True):
        e = EligibilityEvaluation(resident_id=r.id, guideline_version_id=gv.id, outcome=outcome, outcome_class=cls, explanation={"criteria": [{"detail": "text"}]},
                                  input_hash="h", evaluation_date=date(2026, 11, 8), execution_version="t")  # fmt: skip
        s.add(e)
        evs.append(e)
    s.flush()
    cases = []
    for r, e, status in zip(res, evs, ("completed", "completed", "planned"), strict=True):
        c = CareGapCase(resident_id=r.id, evaluation_id=e.id, guideline_version_id=gv.id, care_activity="colorectal_screening", status=status, source_outcome="eligible")
        s.add(c)
        cases.append(c)
    s.flush()
    s.add(CompletionEvidence(care_gap_case_id=cases[0].id, activity="colorectal_screening", source_type="simulated_procedure_record", evidence_reference="x",
                             verification_status="verified", verified_at=T0, synthetic_status="simulated"))  # case 2 is completed WITHOUT evidence
    s.add(CaseStatusHistory(case_id=cases[0].id, to_status="completed"))
    s.add(CaseStatusHistory(case_id=cases[2].id, to_status="identified"))  # the stored status is planned: a replay mismatch
    clinic = Clinic(clinic_key=f"c-{uuid.uuid4().hex[:6]}", name="clinic", district_code="D1")
    s.add(clinic)
    s.flush()
    svc = ClinicService(clinic_id=clinic.id, service_key="colonoscopy", service_name="Colonoscopy", appointment_duration_minutes=30)
    s.add(svc)
    s.flush()
    slot = CapacitySlot(clinic_id=clinic.id, clinic_service_id=svc.id, start_time=T0, end_time=T0 + timedelta(minutes=30), available_capacity=1)
    s.add(slot)
    run = PlanningRun(planning_period_start=T0 - timedelta(days=1), planning_period_end=T0 + timedelta(days=10), configuration_version="c", algorithm_version="a",
                      seed=1, status="completed", config={"max_travel_tier": 1, "num_workers": 1}, solver_parameters={"status": "optimal"},
                      metrics={"hard_constraints": {"checked": ["slot_capacity"], "violations": 0}})  # fmt: skip
    s.add(run)
    s.flush()
    alloc = PlannedAllocation(planning_run_id=run.id, care_gap_case_id=cases[2].id, resident_id=res[2].id, clinic_id=clinic.id, clinic_service_id=svc.id,
                              capacity_slot_id=slot.id, clinic_key=clinic.clinic_key, service_key="colonoscopy", district_code="D1", language="ar",
                              proposed_start_time=T0, proposed_end_time=T0 + timedelta(minutes=30), allocation_status="confirmed")  # fmt: skip
    s.add(alloc)
    s.add(UnallocatedCase(planning_run_id=run.id, care_gap_case_id=cases[1].id, resident_id=res[1].id, district_code="D2", language="hi",
                          reason_code=REASON_CODES[3], explanation="No capacity in the planning horizon."))  # fmt: skip
    s.flush()
    ce = CommunicationEvent(care_gap_case_id=cases[2].id, resident_id=res[2].id, channel="sms", template_key="t", template_version=1, language="ar", body="b")
    s.add(ce)
    s.add(Appointment(care_gap_case_id=cases[2].id, resident_id=res[2].id, clinic_id=clinic.id, clinic_service_id=svc.id, capacity_slot_id=slot.id,
                      planned_allocation_id=alloc.id, start_time=T0, end_time=T0 + timedelta(minutes=30), appointment_status="simulated_confirmed"))  # fmt: skip
    job = ProcessingJob(job_type="evidence.extract_batch", status="completed", processed_items=60, started_at=T0, completed_at=T0 + timedelta(minutes=2))
    s.add(job)
    s.flush()
    inv_ids = []
    for status, cost, fb in (("succeeded", Decimal("0.0007"), False), ("succeeded", Decimal("0.0009"), True)):
        inv = ModelInvocation(task="evidence.extract", provider="claude", model_id="haiku-test", prompt_version="evidence.extract@1", input_tokens=900, output_tokens=200,
                              cost_usd=cost, latency_ms=1200, status=status, fallback_used=fb, job_id=job.id)  # fmt: skip
        s.add(inv)
        inv_ids.append(inv)
    s.flush()
    return {"res": [str(r.id) for r in res], "cases": cases, "run": run, "ds": ds, "job_id": str(job.id), "inv_ids": [str(i.id) for i in inv_ids], "alloc": alloc}


def test_eligibility_reader_and_mode_e(db_session, seeded):
    evals = readers.latest_evaluations(db_session, seeded["res"])
    assert {e["outcome_class"] for e in evals} == {"ineligible", "eligible"} and all(e["explanation_complete"] for e in evals) and "explanation" not in evals[0]
    residents = readers.load_residents(db_session, seeded["res"])
    key = evals[0]["guideline_key"]
    renamed = [{**e, "guideline_key": "colorectal_average_risk"} for e in evals]
    assert key.startswith("colorectal_average_risk")
    items = [{"resident_id": seeded["res"][0], "family": "crc", "concept": "colonoscopy", "status": "up_to_date", "mode": "structured", "fact_type": "colonoscopy"}]
    out = eligibility.evaluate_mode_e(renamed, residents, items, as_of=date(2026, 11, 8))
    # r0: planted up-to-date -> expected already_completed (ineligible), engine agrees; r1/r2: no planted item -> scored only in EL-01b
    assert out["scope"]["pairs_scored"] == 1 and out["EL-01"]["value"] == 1.0 and out["scope"]["pairs_no_planted_item"] == 2 and out["EL-01b"]["value"] == 1.0


def test_planning_readers_and_independent_validator(db_session, seeded):
    runs = readers.completed_planning_runs(db_session, limit=5)
    run = next(r for r in runs if r["id"] == seeded["run"].id)
    rows = readers.planning_rows(db_session, run["id"])
    cap = readers.capacity_by_district(db_session, run["planning_period_start"], run["planning_period_end"])
    cases = readers.cases_for_planning(db_session, [r["care_gap_case_id"] for r in rows["allocations"]])
    assert rows["allocations_total"] == 1 and len(rows["unallocated"]) == 1 and cap["D1"]["seats"] >= 1
    out = planning_eval.evaluate_run(run, rows, cap, existing_cases=cases, allocations_total=rows["allocations_total"], reason_codes=REASON_CODES)
    assert not any(out["violations_by_constraint"].values()) and out["planner_recorded_validation"] and out["unallocated_explained"]["value"] == 1.0
    assert out["solver_status"] == "optimal" and out["allocation_rate"]["denominator"] == 2


def test_closure_reader_finds_completion_without_evidence_and_replay_mismatch(db_session, seeded):
    data = readers.closure_rows(db_session, seeded["res"])
    out = closure_eval.evaluate_closure(data, readers.load_residents(db_session, seeded["res"]))
    assert out["LC-01"]["value"] == 1 and out["LC-01"]["n"] == 2  # case 2 is `completed` with no verified evidence
    assert out["LC-03"]["value"] == 1 and out["LC-05"]["value"] == 0 and out["EQ-08"]["value"] == 1.0
    assert out["LC-04"]["funnel"]["with_appointment"] == 1 and out["LC-04"]["funnel"]["with_simulated_outreach"] == 1


def test_invalid_closure_probe_uses_the_real_service_and_changes_nothing(db_session, seeded):
    from sqlalchemy.orm import sessionmaker

    sessions = sessionmaker(bind=db_session.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False)
    probe = closure_eval.probe_invalid_closures(sessions, sample=50)
    assert probe["attempts"] >= 1 and probe["accepted_in_error"] == 0 and probe["rejected"] == probe["attempts"]
    assert set(probe["error_codes"]) <= {"VALIDATION_ERROR", "INVALID_TRANSITION"}
    db_session.expire_all()
    assert db_session.get(CareGapCase, seeded["cases"][2].id).status == "planned"  # nothing persisted


def test_cost_and_job_readers(db_session, seeded):
    inv = readers.load_invocations(db_session)
    mine = [i for i in inv if i["job_id"] == seeded["job_id"]]
    assert len(mine) == 2
    jobs = [j for j in readers.load_extraction_jobs(db_session) if j["id"] == seeded["job_id"]]
    out = perf.evaluate_perf(mine, jobs, linked_invocation_ids={seeded["inv_ids"][0]}, documents_evaluated=10, documents_failed=0,
                             residents_in_cohort=5, residents_processed=4)  # fmt: skip
    assert out["PF-01"]["total_cost_usd"] == pytest.approx(0.0016) and out["PF-01"]["value"] == pytest.approx(0.0016 / 0.06, abs=1e-5)  # per 1,000 of the 60 documents
    assert out["PF-01s"]["total_cost_usd"] == pytest.approx(0.0007) and out["PF-01s"]["calls"] == 1  # only the call linked to a fact
    assert out["PF-02a"]["value"] == pytest.approx(0.0016 / 0.004, abs=1e-5) and out["PF-02b"]["value"] == pytest.approx(0.0016 / 0.005, abs=1e-5)
    assert out["PF-05c"]["numerator"] == 1 and out["PF-06"]["value"] == 1.0 and out["PF-04"]["overall"]["n"] == 2
    assert out["PF-03"]["value"] == pytest.approx(30.0) and out["PF-01"]["by_provider_model_task"]["claude/haiku-test/evidence.extract"]["calls"] == 2  # 60 documents in 2 minutes
