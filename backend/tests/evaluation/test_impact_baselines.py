"""Impact baselines (gap audit B02, B08, B09 data half) in the evaluation package: the rules only baseline, the first come
planner baseline, the report digest and the stored copy. Pure tests use the authored guideline regression cases and a tiny
planning instance; the stored copy test runs on DB `anticipa_test` (rolled back)."""
# ruff: noqa: E501

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from anticipa.analytics import impact as serving
from anticipa.evaluation import impact
from anticipa.evaluation.eligibility import DRAFT_PATH, _fact_for
from anticipa.guidelines.evaluator import ResidentFacts, ResidentInputs, shift_back
from anticipa.guidelines.rules import parse_guideline_definition, parse_rule
from anticipa.planning.domain import PlanConfig, PlannableCase, PoolSpec, SlotSpec
from anticipa.planning.solver import plan

_DRAFT = json.loads(DRAFT_PATH.read_text(encoding="utf-8"))
ON = date.fromisoformat(_DRAFT.get("evaluation_date") or (_DRAFT.get("conventions") or {}).get("test_evaluation_date") or "2026-11-08")  # as eligibility.run_regression

# ------------------------------------------------------------------------------------------------ rules baseline
@pytest.fixture(scope="module")
def authored():
    """guideline key -> (guideline-like object for `paired_rows`, authored regression cases by id), from the real approved rule drafts."""
    library, out = _DRAFT["selector_library"], {}
    for g in _DRAFT["guidelines"]:
        names: set[str] = set()
        for raw in g["rules"]:
            names |= parse_rule(raw).selectors_used()
        definition = parse_guideline_definition(g["rules"], {n: library[n] for n in sorted(names)}, g.get("decision_policy"))
        lg = SimpleNamespace(guideline_key=g["guideline_key"], definition=definition, sources=g.get("sources", []), eligible_explanation=g.get("eligible_explanation"))
        out[g["guideline_key"]] = (lg, {c["id"]: c for c in g["test_cases"]}, library)
    return out


def inputs_of(case, library) -> ResidentInputs:
    birth = shift_back(ON, years=int(case["age"])) if case.get("age") is not None else None
    facts = [_fact_for(i, library) for i in case["evidence"]]  # every authored item is a note derived fact (source 'fact')
    latest = None if case.get("no_coverage") else shift_back(ON, months=6)
    return ResidentInputs(ResidentFacts(uuid.uuid4(), birth, None, case.get("sex")), facts, latest)


def test_with_notes_is_the_engine_as_authored_and_the_baseline_ignores_notes(authored):
    for key, (lg, cases, library) in authored.items():
        for cid, case in cases.items():
            (row,) = impact.paired_rows(inputs_of(case, library), [lg], ON)
            assert row["now"] == case["expected"]["outcome"], (key, cid)  # unchanged engine behaviour with notes
            full = inputs_of(case, library)
            nothing = ResidentInputs(full.resident, [], full.latest_record_date)
            (blank,) = impact.paired_rows(nothing, [lg], ON)
            assert row["base"] == blank["now"], (key, cid)  # the baseline sees exactly what a record free resident shows


def test_notes_prevent_duplicate_outreach_and_open_family_history_pathways(authored):
    lg, cases, library = authored["breast_cancer_screening"]
    (row,) = impact.paired_rows(inputs_of(cases["t08"], library), [lg], ON)  # mammogram completed 2025-03-01, documented in a note only
    assert (row["base"], row["now"]) == ("eligible", "already_completed")
    assert impact.change_kind(row["base"], row["now"]) == "duplicate_outreach_avoided"
    lg, cases, library = authored["brca_risk_assessment"]
    (row,) = impact.paired_rows(inputs_of(cases["t11"], library), [lg], ON)  # relative with breast or ovarian cancer found in a note
    assert (row["base"], row["now"]) == ("not_eligible", "eligible")
    assert impact.change_kind(row["base"], row["now"]) == "newly_due"


def row(rid, key, base, now, **kw):
    cls = {"eligible": "eligible", "requires_clinical_review": "indeterminate", "already_completed": "ineligible", "not_eligible": "ineligible", "insufficient_evidence": "indeterminate"}
    return {"resident_id": rid, "key": key, "base": base, "base_class": cls[base], "now": now, "now_class": cls[now], "base_reason": None, "now_reason": "r", **kw}


def test_uplift_summary_counts_each_kind_once():
    rows = [
        row("a", "g1", "not_eligible", "eligible"), row("a", "g2", "eligible", "eligible"), row("b", "g1", "eligible", "already_completed"),
        row("c", "g1", "not_eligible", "requires_clinical_review"), row("d", "g1", "eligible", "requires_clinical_review"),
        row("e", "g2", "eligible", "not_eligible"), row("f", "g2", "requires_clinical_review", "already_completed"), row("g", "g2", "already_completed", "eligible"),
        row("h", "g2", "eligible", "insufficient_evidence"), row("i", "g2", "not_eligible", "insufficient_evidence"), row("j", "g2", "already_completed", "not_eligible"),
    ]
    s = impact.uplift_summary(rows)
    assert s["all"]["pairs"] == 11 and sum(s["all"]["changes"].values()) == 11
    assert s["all"]["changes"] == {"newly_due": 2, "newly_review": 1, "due_to_review": 1, "duplicate_outreach_avoided": 1, "outreach_removed_not_eligible": 1,
                                   "review_closed_by_notes": 1, "paused_for_missing_information": 1, "newly_missing_information": 1,
                                   "pathway_or_status_change": 1, "unchanged": 1}  # fmt: skip
    assert s["all"]["case_opened_only_because_of_notes"] == 3  # a/g1, c/g1, g/g2: no case before, a case now
    assert s["all"]["residents_with_case_opened_only_because_of_notes"] == 3
    assert s["all"]["case_removed_because_of_notes"] == 4  # b, e, f and h leave the case set
    assert s["g1"]["pairs"] == 4 and {"from": "not_eligible", "to": "eligible", "count": 1, "top_reasons_with_notes": {"r": 1}} in s["g1"]["transitions"]


def test_scoring_against_truth_separates_missed_and_unnecessary_outreach():
    rows = [row("a", "g", "eligible", "already_completed"), row("b", "g", "eligible", "eligible"), row("c", "g", "eligible", "not_eligible"), row("d", "g", "not_eligible", "requires_clinical_review")]
    truth = {("a", "g"): "ineligible", ("b", "g"): "eligible", ("c", "g"): "eligible", ("d", "g"): "indeterminate"}
    s = impact.score_against(rows, truth)
    assert s["rules_only"]["truly_not_due"] == {"pairs": 1, "unnecessary_outreach": 1, "correctly_not_due": 0, "sent_to_review": 0}  # no note, so a done screening is chased again
    assert s["with_notes"]["truly_not_due"]["unnecessary_outreach"] == 0
    assert s["rules_only"]["truly_due"] == {"pairs": 2, "found_as_due": 2, "missed_as_not_due": 0, "sent_to_review": 0}
    assert s["with_notes"]["truly_due"]["missed_as_not_due"] == 1  # reading can also hurt: reported, not hidden
    assert s["rules_only"]["expected_review"]["firm_answer_instead"] == 1 and s["with_notes"]["expected_review"]["flagged_for_review"] == 1
    assert s["with_notes"]["agreement"]["numerator"] == 3 and s["rules_only"]["agreement"]["numerator"] == 2


def test_planted_labels_become_engine_facts_only_when_expected_present():
    rec = {"label_id": "L1", "expected_present": True, "category": "family_history", "fact_type": "ovarian_cancer", "subject_type": "family_member", "relationship": "mother",
           "related_person_key": "docrel:r1:mother", "assertion": "positive", "temporal": "historical", "expected_validation": "accepted", "age_at_onset": 46, "event_date": None}  # fmt: skip
    f = impact.planted_fact(rec)
    assert (f.source, f.subject_key, f.relationship, f.onset, f.validation, f.date_start) == ("fact", "docrel:r1:mother", "mother", 46, "accepted", None)
    assert impact.planted_fact({**rec, "expected_present": False}) is None
    assert impact.planted_fact({**rec, "related_person_key": None}).subject_key == "mother"


# ------------------------------------------------------------------------------------------------ planner baseline
T0 = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
CFG = PlanConfig(horizon_start=datetime(2026, 11, 10, tzinfo=UTC), horizon_end=datetime(2026, 11, 17, tzinfo=UTC), time_limit_seconds=20)


def pool(key, district, service="svc"):
    return PoolSpec(uuid.uuid5(uuid.NAMESPACE_DNS, f"pool:{key}"), uuid.uuid5(uuid.NAMESPACE_DNS, f"clinic:{key}"), key, district, service, 30)


def slot(p, hour, free, day=0):
    start = T0.replace(hour=hour) + timedelta(days=day)
    return SlotSpec(uuid.uuid5(uuid.NAMESPACE_DNS, f"slot:{p.clinic_service_id}:{start}"), p.clinic_service_id, start, start + timedelta(minutes=30), free)


def case(n, district, priority="routine", resident=None):
    return PlannableCase(uuid.uuid5(uuid.NAMESPACE_URL, f"case:{n}"), uuid.uuid5(uuid.NAMESPACE_URL, f"resident:{resident if resident is not None else n}"),
                         "svc", priority, district, "en", T0 + timedelta(minutes=n))  # fmt: skip


def test_first_come_serves_arrival_order_nearest_clinic_first_and_valid():
    a, b = pool("a", "D1"), pool("b", "D2")
    slots = [slot(a, 9, 1), slot(a, 10, 1), slot(b, 9, 2)]
    cases = [case(1, "D1"), case(2, "D1"), case(3, "D1"), case(4, "D2"), case(5, "D2")]
    prep = impact.prepare(cases, [a, b], slots, CFG)
    placed = impact.greedy_assign(cases, prep, CFG)
    assert [placed[c.case_id].clinic_service_id == a.clinic_service_id for c in cases[:2]] == [True, True]  # own clinic while it has seats
    assert placed[cases[2].case_id].clinic_service_id == b.clinic_service_id  # spills to the adjacent district
    assert placed[cases[3].case_id].clinic_service_id == b.clinic_service_id and cases[4].case_id not in placed  # arrival order decides who is left out
    m = impact.plan_metrics(cases, placed, prep, CFG, [a, b], slots)
    assert m["allocated"] == 4 and m["hard_constraint_violations"]["count"] == 0
    assert m["travel"]["own_district"] == 3 and m["travel"]["adjacent_district"] == 1


def test_first_come_ignores_priority_that_the_planner_serves():
    a = pool("a", "D1")
    slots = [slot(a, 9, 1), slot(a, 10, 1), slot(a, 11, 1)]
    cases = [case(1, "D1"), case(2, "D1"), case(3, "D1"), case(4, "D1", "urgent"), case(5, "D1", "urgent")]  # the urgent cases arrive last
    prep = impact.prepare(cases, [a], slots, CFG)
    first = impact.plan_metrics(cases, impact.greedy_assign(cases, prep, CFG), prep, CFG, [a], slots)
    by_priority = impact.plan_metrics(cases, impact.greedy_assign(cases, prep, CFG, by_priority=True), prep, CFG, [a], slots)
    res = plan(cases, [a], slots, CFG)
    optimal = impact.plan_metrics(cases, {x.case_id: next(s for s in slots if s.slot_id == x.slot_id) for x in res.allocations}, prep, CFG, [a], slots)
    assert first["coverage_by_priority"]["urgent"]["allocated"] == 0
    assert by_priority["coverage_by_priority"]["urgent"]["allocated"] == optimal["coverage_by_priority"]["urgent"]["allocated"] == 2
    assert optimal["priority_weighted_coverage"]["value"] > first["priority_weighted_coverage"]["value"]
    assert optimal["hard_constraint_violations"]["count"] == first["hard_constraint_violations"]["count"] == 0


def test_first_come_never_double_books_a_resident():
    a = pool("a", "D1")
    slots = [slot(a, 9, 2), slot(a, 10, 1)]
    cases = [case(1, "D1", resident=7), case(2, "D1", resident=7)]  # one resident, two cases, same service
    prep = impact.prepare(cases, [a], slots, CFG)
    placed = impact.greedy_assign(cases, prep, CFG)
    assert len(placed) == 2 and placed[cases[0].case_id].start != placed[cases[1].case_id].start
    assert impact.plan_metrics(cases, placed, prep, CFG, [a], slots)["hard_constraint_violations"]["count"] == 0


def test_a_stored_copy_serves_a_deployment_without_the_data_folder(db_session, tmp_path, monkeypatch):
    sessions = sessionmaker(bind=db_session.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False)
    monkeypatch.setattr(serving, "REPORTS_DIR", tmp_path)  # no report file on this machine
    serving._CACHE.clear()
    with pytest.raises(FileNotFoundError):
        serving.load_report(db_session)
    report = {"headlines": ["one", "two"], "generated_at": datetime.now(UTC).isoformat(), "rules_baseline": {"seconds": 1.5}, "planner_baseline": {"seconds": 2.5}}
    impact.store_report(sessions, report)
    impact.store_report(sessions, {**report, "headlines": ["newer"]})  # an upsert, not a second row
    assert serving.load_report(db_session)["headlines"] == ["newer"]


def test_digest_ignores_time_and_duration_but_not_figures():
    a = {"headlines": ["x"], "generated_at": "t1", "rules_baseline": {"seconds": 1, "n": 5}}
    b = {"headlines": ["x"], "generated_at": "t2", "rules_baseline": {"seconds": 99, "n": 5}}
    assert impact.digest(a) == impact.digest(b) != impact.digest({**a, "rules_baseline": {"seconds": 1, "n": 6}})
