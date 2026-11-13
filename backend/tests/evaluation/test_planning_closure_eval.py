"""T-12 (planner validator with a known violation of every hard-constraint type), equity maths and T-13-style closure checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from anticipa.evaluation import closure_eval
from anticipa.evaluation import planning_eval as pe

T0 = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)
START, END = T0 - timedelta(days=1), T0 + timedelta(days=30)
DISTRICTS = ["D1", "D2", "D3", "D4", "D5", "D6"]


def alloc(i, **over):
    base = {"id": f"a{i}", "care_gap_case_id": f"c{i}", "resident_id": f"r{i}", "service_key": "mammography", "district_code": "D1", "language": "en",
            "allocation_status": "proposed", "proposed_start_time": T0, "proposed_end_time": T0 + timedelta(minutes=30), "capacity_slot_id": f"s{i}",
            "slot_start": T0, "slot_end": T0 + timedelta(minutes=30), "available_capacity": 2, "slot_status": "open", "slot_service_key": "mammography",
            "service_active": True, "duration_minutes": 30, "clinic_status": "open", "clinic_district": "D1"}  # fmt: skip
    base.update(over)
    return base


def ctx(rows, existing=None, total=None):
    return pe.Ctx(START, END, 1, DISTRICTS, {r["care_gap_case_id"] for r in rows} if existing is None else existing, len(rows) if total is None else total)


def violations(rows, **kw):
    return {k: v for k, v in pe.validate(rows, ctx(rows, **kw)).items() if v}


def test_clean_plan_has_no_violations():
    assert violations([alloc(i) for i in range(1, 6)]) == {}


@pytest.mark.parametrize(
    ("name", "rows", "kw"),
    [
        ("slot_capacity", [alloc(i, capacity_slot_id="s1") for i in (1, 2, 3)], {}),  # 3 seats used, 2 available
        ("service_match", [alloc(1, slot_service_key="pap_smear")], {}),
        ("slot_availability", [alloc(1, slot_status="closed")], {}),
        ("slot_availability", [alloc(1, clinic_status="closed")], {}),
        ("slot_availability", [alloc(1, service_active=False)], {}),
        ("appointment_duration", [alloc(1, slot_end=T0 + timedelta(minutes=20), proposed_end_time=T0 + timedelta(minutes=20))], {}),
        ("planning_horizon", [alloc(1, slot_start=START - timedelta(days=2), proposed_start_time=START - timedelta(days=2))], {}),
        ("one_allocation_per_case", [alloc(1), alloc(2, care_gap_case_id="c1")], {}),
        ("travel_eligibility", [alloc(1, clinic_district="D3")], {}),  # ring distance 2 > max tier 1
        ("resident_time_conflict", [alloc(1), alloc(2, resident_id="r1")], {}),  # same resident, overlapping slots
        ("case_in_scope", [alloc(1)], {"existing": set()}),
        ("slot_exists", [alloc(1)], {"total": 2}),
        ("snapshot_matches_slot", [alloc(1, proposed_start_time=T0 + timedelta(minutes=5))], {}),
        ("consent_respected", [alloc(1, consent_denied=True)], {}),  # resident opted out of every channel (ADR-070)
    ],
)
def test_validator_detects_each_hard_constraint_independently_of_the_solver(name, rows, kw):
    got = violations(rows, **kw)
    assert name in got and len(got[name]) >= 1, got


def test_adjacent_district_is_within_tier_one_and_missing_district_is_a_violation():
    assert "travel_eligibility" not in violations([alloc(1, clinic_district="D2")])
    assert "travel_eligibility" in violations([alloc(1, district_code=None)])


def test_every_constraint_the_planner_declares_has_a_validator_here():
    assert set(pe.declared_constraints()) <= set(pe.VALIDATORS)
    agg = pe.aggregate([_run_result()], ["slot_capacity", "invented_constraint"])
    assert agg["CP-01"]["declared_constraints_without_validator"] == ["invented_constraint"]


def _run_result(allocs=None, unalloc=None, hc=None):
    run = {"id": "run-1", "planning_period_start": START, "planning_period_end": END, "config": {"max_travel_tier": 1, "num_workers": 1},
           "solver_parameters": {"status": "optimal"}, "metrics": {"hard_constraints": hc if hc is not None else {"checked": ["slot_capacity"], "violations": 0}, "timings_s": {"solve": 1.0}}}  # fmt: skip
    allocs = allocs if allocs is not None else [alloc(i) for i in range(1, 5)]
    rows = {"allocations": allocs, "unallocated": unalloc or []}
    cap = {"D1": {"seats": 10, "seat_minutes": 300}}
    return pe.evaluate_run(run, rows, cap, existing_cases={a["care_gap_case_id"] for a in allocs}, allocations_total=len(allocs),
                           reason_codes=("capacity_exhausted", "no_capacity_in_horizon"))  # fmt: skip


def test_run_metrics_utilisation_allocation_rate_and_explanation_coverage():
    unalloc = [{"care_gap_case_id": f"u{i}", "resident_id": f"u{i}", "district_code": "D2", "language": "hi", "reason_code": code, "explanation": text, "service_key": "mammography"}
               for i, (code, text) in enumerate([("capacity_exhausted", "No seats left."), ("made_up_code", "x"), ("no_capacity_in_horizon", "")])]  # fmt: skip
    r = _run_result(unalloc=unalloc)
    assert r["slot_utilisation"]["numerator"] == 4 and r["slot_utilisation"]["denominator"] == 10
    assert r["time_utilisation"]["numerator"] == 120 and r["time_utilisation"]["denominator"] == 300
    assert (r["allocation_rate"]["numerator"], r["allocation_rate"]["denominator"]) == (4, 7)
    assert (r["unallocated_explained"]["numerator"], r["unallocated_explained"]["denominator"]) == (1, 3)  # invalid code and empty text do not count
    assert r["planner_recorded_validation"] is True and r["solver_status"] == "optimal" and not any(r["violations_by_constraint"].values())


def test_unvalidated_plan_is_counted_cp09_and_aggregate_sums_cp01():
    bad = _run_result(allocs=[alloc(i, capacity_slot_id="s1") for i in (1, 2, 3)], hc={})
    agg = pe.aggregate([bad, _run_result()], pe.declared_constraints())
    assert agg["CP-01"]["value"] == 1 and agg["CP-01"]["by_constraint"]["slot_capacity"] == 1 and agg["CP-09"]["value"] == 1
    assert pe.aggregate([], [])["status"] == "not_evaluated"


def test_equity_parity_gap_ratio_waits_and_low_n_exclusion():
    allocs = [alloc(i, district_code="D1") for i in range(40)] + [alloc(100 + i, district_code="D2", proposed_start_time=T0 + timedelta(days=2)) for i in range(20)] + [alloc(200 + i, district_code="D3") for i in range(3)]
    unalloc = [{"district_code": "D1", "language": "en"}] * 10 + [{"district_code": "D2", "language": "en"}] * 30 + [{"district_code": "D3", "language": "en"}] * 2
    eq = pe.equity(allocs, unalloc, T0, {"D1": {"seats": 100, "seat_minutes": 0}})
    d = eq["district_code"]
    assert d["allocation_rate_by_group"]["D1"]["value"] == 0.8 and d["allocation_rate_by_group"]["D2"]["value"] == 0.4
    assert "low_n" in d["allocation_rate_by_group"]["D3"]["flags"] and d["groups_used"] == 2  # D3 has only 5 cases
    assert d["parity_gap_points"] == 40.0 and d["parity_ratio"] == 0.5
    assert d["median_wait_days"]["D2"] == 2.0 and eq["capacity_to_demand_by_district"]["D1"]["ratio"] == 2.0


# ----------------------------------------------------------------------------------------------- closure
def rows(**over):
    base = {"cases": [], "evidence": [], "history_last": {}, "communications": [], "appointments": []}
    base.update(over)
    return base


def test_closure_without_verified_evidence_for_the_cases_activity_is_counted():
    cases = [{"id": f"c{i}", "resident_id": f"r{i}", "status": "completed", "care_activity": "mammography", "closed_at": None, "synthetic_status": "synthetic"} for i in (1, 2, 3)]
    cases.append({"id": "c4", "resident_id": "r4", "status": "planned", "care_activity": "mammography", "closed_at": None, "synthetic_status": "synthetic"})
    evidence = [{"care_gap_case_id": "c1", "activity": "mammography", "verification_status": "verified", "synthetic_status": "simulated"},
                {"care_gap_case_id": "c2", "activity": "mammography", "verification_status": "pending", "synthetic_status": "simulated"},  # not verified
                {"care_gap_case_id": "c3", "activity": "colonoscopy", "verification_status": "verified", "synthetic_status": "simulated"}]  # another activity
    out = closure_eval.evaluate_closure(rows(cases=cases, evidence=evidence), {})
    assert out["LC-01"]["value"] == 2 and out["LC-01"]["n"] == 3 and out["LC-01"]["case_ids"] == ["c2", "c3"]
    assert out["LC-04"]["funnel"]["completed"] == 3


def test_audit_replay_simulated_labelling_and_message_language():
    cases = [{"id": "c1", "resident_id": "r1", "status": "planned", "care_activity": "x", "closed_at": None, "synthetic_status": "synthetic"},
             {"id": "c2", "resident_id": "r2", "status": "planned", "care_activity": "x", "closed_at": None, "synthetic_status": "synthetic"}]  # fmt: skip
    comm = [{"care_gap_case_id": "c1", "synthetic_status": "simulated", "language": "hi", "resident_id": "r1"},
            {"care_gap_case_id": "c2", "synthetic_status": "synthetic", "language": "en", "resident_id": "r2"}]  # not labelled simulated; wrong language
    out = closure_eval.evaluate_closure(rows(cases=cases, communications=comm, history_last={"c1": "planned", "c2": "completed"}),
                                        {"r1": {"language": "hi"}, "r2": {"language": "ar"}})  # fmt: skip
    assert out["LC-03"]["value"] == 1 and out["LC-03"]["case_ids"] == ["c2"] and out["LC-05"]["value"] == 1
    assert (out["EQ-08"]["numerator"], out["EQ-08"]["denominator"]) == (1, 2)


def test_probe_metric_not_evaluated_without_probe_targets():
    assert closure_eval.probe_metric({"attempts": 0, "rejected": 0, "error_codes": {}})["status"] == "not_evaluated"
    full = closure_eval.probe_metric({"attempts": 4, "rejected": 4, "accepted_in_error": 0, "error_codes": {"VALIDATION_ERROR": 4}})
    assert full["value"] == 1.0 and full["error_codes"] == {"VALIDATION_ERROR": 4}
