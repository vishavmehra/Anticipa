"""Eligibility evaluation (09 §7.5): the planted-ground-truth oracle, Mode E agreement maths and the Mode R regression replay."""

from __future__ import annotations

from datetime import date

import pytest

from anticipa.evaluation import eligibility as el

ON = date(2026, 11, 8)


def born(age: int) -> date:
    return date(ON.year - age, 1, 1)  # birthday already passed: exactly `age` completed years on ON


def item(family, concept, status, mode="structured", fact_type=None, label=None):
    return {"family": family, "concept": concept, "status": status, "mode": mode, "fact_type": fact_type or concept, "document_label_id": label}


def exp(key, age, sex, *items):
    return el.expected_outcome(key, born(age), sex, list(items), ON)


def test_age_is_completed_years_on_the_evaluation_date():
    assert el.age_on(date(1974, 11, 9), ON) == 51 and el.age_on(date(1974, 11, 8), ON) == 52


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("colorectal_average_risk", 44, "male"), ("ineligible", "demographic")),
        (("colorectal_average_risk", 86, "male"), ("ineligible", "demographic")),
        (("colorectal_average_risk", 52, "male", item("crc", "colonoscopy", "up_to_date")), ("ineligible", "planted_completion")),
        (("colorectal_average_risk", 52, "male", item("crc", "fit", "stale")), ("eligible", "planted_stale")),
        (("colorectal_average_risk", 52, "male"), ("eligible", "no_planted_item")),
        (("colorectal_average_risk", 52, "male", item("exclusion", "total_colectomy", "exclusion")), ("indeterminate", "planted_exclusion")),
        (("colorectal_average_risk", 80, "male"), ("indeterminate", "demographic")),
        (("breast_cancer_screening", 60, "male"), ("ineligible", "demographic")),
        (("breast_cancer_screening", 39, "female"), ("ineligible", "demographic")),
        (("breast_cancer_screening", 60, "female", item("breast", "mammography", "up_to_date")), ("ineligible", "planted_completion")),
        (("breast_cancer_screening", 60, "female", item("exclusion", "bilateral_mastectomy", "exclusion")), ("indeterminate", "planted_exclusion")),
        (("cervical_cancer_screening", 20, "female"), ("ineligible", "demographic")),
        (("cervical_cancer_screening", 50, "female", item("exclusion", "hysterectomy", "exclusion", fact_type="total_hysterectomy")), ("ineligible", "planted_exclusion")),
        (("cervical_cancer_screening", 50, "female", item("exclusion", "hysterectomy", "exclusion")), ("indeterminate", "planted_exclusion")),
        (("cervical_cancer_screening", 27, "female", item("cervical", "hpv_test", "up_to_date")), ("eligible", "no_planted_item")),  # HPV alone does not count before 30
        (("cervical_cancer_screening", 35, "female", item("cervical", "hpv_test", "up_to_date")), ("ineligible", "planted_completion")),
        (("cervical_cancer_screening", 70, "female", item("cervical", "pap_smear", "up_to_date")), ("ineligible", "planted_completion")),
        (("cervical_cancer_screening", 70, "female"), ("indeterminate", "demographic")),
    ],
)
def test_oracle_expected_outcomes(args, expected):
    got = exp(*args)
    assert (got[0], got[2]) == expected


def test_oracle_abstains_when_the_precedence_is_unclear_or_birth_date_is_missing():
    assert exp("colorectal_average_risk", 80, "male", item("crc", "colonoscopy", "up_to_date")) is None  # selective band + planted completion
    assert el.expected_outcome("colorectal_average_risk", None, "male", [], ON) is None


def ev(rid, key="colorectal_average_risk", cls="ineligible", outcome="already_completed", when=ON, detail="criterion text"):
    return {"resident_id": rid, "guideline_key": key, "outcome": outcome, "outcome_class": cls, "reason_code": None, "evaluation_date": when,
            "explanation": {"criteria": [{"criterion_id": "x", "detail": detail}]}}  # fmt: skip


def test_mode_e_agreement_confusion_rates_and_disagreement_causes():
    res = {f"r{i}": {"birth_date": born(52), "sex": "male", "language": "en" if i % 2 else "hi", "district": "D1"} for i in range(1, 14)}
    items = [item("crc", "colonoscopy", "up_to_date") | {"resident_id": f"r{i}"} for i in range(1, 7)]  # 6 structured completions
    items += [item("crc", "colonoscopy", "stale") | {"resident_id": f"r{i}"} for i in (7, 8)]  # 2 stale
    items += [item("crc", "fit", "up_to_date", "note_only", label="L9") | {"resident_id": "r9"},
              item("crc", "fit", "up_to_date", "note_only", label="L10") | {"resident_id": "r10"}]  # fmt: skip
    evals = [ev(f"r{i}") for i in range(1, 6)] + [ev("r6", cls="eligible", outcome="eligible")]  # one false-eligible on a structured item
    evals += [ev(f"r{i}", cls="eligible", outcome="eligible") for i in (7, 8)]
    evals += [ev("r9", cls="eligible", outcome="eligible"), ev("r10", cls="eligible", outcome="eligible")]  # note-only completions missed
    evals += [ev("r11", cls="eligible", outcome="eligible"),  # no planted item: scored only in EL-01b
              ev("r12"),  # no planted item either, but the engine says already completed (a Synthea native record): EL-01b only
              ev("r13", key="brca_risk_assessment")]  # no oracle for this guideline
    out = el.evaluate_mode_e(evals, res, items, as_of=ON, matched_label_ids={"L10"})
    assert out["scope"]["pairs_scored"] == 10 and out["scope"]["pairs_no_planted_item"] == 2
    assert out["scope"]["skipped"] == {"guideline_without_oracle": 1}
    assert (out["EL-01"]["numerator"], out["EL-01"]["denominator"]) == (7, 10)  # 5 ineligible + 2 stale-eligible agree
    assert (out["EL-01b"]["numerator"], out["EL-01b"]["denominator"]) == (8, 12)
    assert out["confusion_expected_by_returned"]["ineligible"] == {"eligible": 3, "ineligible": 5, "indeterminate": 0, "error": 0}
    assert (out["EL-04"]["numerator"], out["EL-04"]["denominator"]) == (3, 8)  # false-eligible
    assert out["EL-03"]["numerator"] == 0 and out["EL-03"]["denominator"] == 2  # no missed care gap
    assert out["EL-05b"]["status"] == "not_applicable"  # nothing was expected indeterminate
    assert out["EL-11"]["causes"] == {"note_only_completion_extracted_outcome_differs": 1, "note_only_completion_not_extracted": 1, "structured_item_outcome_differs": 1}
    assert out["EL-01"]["by_item_mode"]["note_only"]["value"] == 0.0 and out["EL-01"]["by_basis"]["planted_stale"]["value"] == 1.0
    assert out["EL-09"]["value"] == 1.0


def test_oracle_follows_the_engines_evaluation_date_not_the_planted_status():
    """A colonoscopy 10 years and 2 days before 2026-11-12 is overdue then but was 'up to date' on the ground truth date
    (2026-11-08): the expected outcome is derived from the event date at each evaluation's own date."""
    colo = item("crc", "colonoscopy", "up_to_date") | {"event_date": "2016-11-10"}
    assert el.expected_outcome("colorectal_average_risk", born(52), "male", [colo], date(2026, 11, 8))[:2] == ("ineligible", "already_completed")
    assert el.expected_outcome("colorectal_average_risk", born(52), "male", [colo], date(2026, 11, 12))[:2] == ("eligible", "overdue")
    assert el.years_before(date(2026, 11, 8), 10) == date(2016, 11, 8)
    assert el.status_on(colo | {"event_date": "2016-11-08"}, date(2026, 11, 8)) == "stale"  # exactly 10 years ago: overdue (window exclusive)
    assert el.status_on(colo | {"event_date": "2016-11-09"}, date(2026, 11, 8)) == "up_to_date"
    res = {"a": {"birth_date": born(52), "sex": "male", "language": "en", "district": "D1"}}
    out = el.evaluate_mode_e([ev("a", cls="eligible", outcome="eligible", when=date(2026, 11, 12))], res, [colo | {"resident_id": "a"}], as_of=ON)
    assert out["EL-01"]["value"] == 1.0 and out["scope"]["evaluation_dates"] == {"2026-11-12": 1} and out["scope"]["ground_truth_as_of"] == "2026-11-08"


def test_indeterminate_handling_unsafe_determinate_and_over_indeterminate():
    res = {"a": {"birth_date": born(80), "sex": "male", "language": "en", "district": "D1"}, "b": {"birth_date": born(52), "sex": "male", "language": "en", "district": "D1"},
           "c": {"birth_date": born(52), "sex": "male", "language": "en", "district": "D1"}}  # fmt: skip
    items = [item("crc", "colonoscopy", "stale") | {"resident_id": "b"}, item("crc", "colonoscopy", "stale") | {"resident_id": "c"}]
    evals = [ev("a", cls="eligible", outcome="eligible"),  # expected indeterminate (age 80), engine is falsely certain
             ev("b", cls="indeterminate", outcome="insufficient_evidence"),  # expected eligible: review burden
             ev("c", cls="ineligible", outcome="not_eligible")]  # expected eligible: missed care gap
    out = el.evaluate_mode_e(evals, res, items, as_of=ON)
    assert out["EL-05b"]["value"] == 1.0 and out["EL-05a"]["value"] == 0.0
    assert out["EL-03"]["numerator"] == 1 and out["EL-03"]["denominator"] == 2
    assert out["EL-06"]["numerator"] == 1 and out["EL-06"]["denominator"] == 2


def test_incomplete_explanations_are_counted():
    res = {"a": {"birth_date": born(52), "sex": "male", "language": "en", "district": "D1"}}
    out = el.evaluate_mode_e([ev("a", detail=""), ev("a", key="x")], res, [], as_of=ON)
    assert out["EL-09"]["numerator"] == 1 and out["EL-09"]["denominator"] == 2  # the empty-detail explanation is the incomplete one


def test_mode_r_replays_every_authored_regression_case_deterministically():
    reg = el.run_regression()
    assert reg["cases"] >= 181 and 0 <= reg["passed"] <= reg["cases"]
    assert reg["determinism_differences"] == 0 and reg["determinism_runs_per_case"] == 3
    assert len(reg["failures"]) == reg["cases"] - reg["passed"]
    metrics = el.regression_metrics(reg)
    assert metrics["EL-02"]["numerator"] == reg["passed"] and metrics["EL-08"]["value"] == 0
    assert sum(v["cases"] for v in reg["per_guideline"].values()) == reg["cases"]
