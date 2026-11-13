"""Deterministic case priority (ADR-073): the rule set, priority at case creation (with the rule recorded in the history),
and the batched recompute for open cases (audit event per batch, updated_at untouched, terminal cases untouched, idempotent,
refused beside case creating jobs). Integration, DB `anticipa_test`, rolled back."""
# ruff: noqa: E501

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from anticipa.caregaps import priority as prio
from anticipa.caregaps import service
from anticipa.caregaps.models import CareGapCase, CaseStatusHistory
from anticipa.core.models import AuditEvent
from anticipa.guidelines.models import EligibilityEvaluation
from anticipa.jobs.models import ProcessingJob

ON = date(2026, 11, 8)


# ------------------------------------------------------------------------------------------------ the rule set
@pytest.mark.parametrize(
    ("activity", "outcome", "conflicts", "floor", "expected"),
    [
        ("brca_risk_assessment", "eligible", False, "routine", ("high", "family_history_pathway")),
        ("brca_genetic_counseling_referral", "eligible", False, "routine", ("high", "family_history_pathway")),
        ("colorectal_family_history", "eligible", False, "routine", ("high", "family_history_pathway")),
        ("colorectal_family_history", "requires_clinical_review", True, "routine", ("high", "family_history_pathway")),  # first match wins
        ("breast_cancer_screening", "requires_clinical_review", False, "routine", ("high", "clinical_review_needed")),
        ("breast_cancer_screening", "requires_clinical_review", True, "routine", ("high", "clinical_review_needed")),
        ("breast_cancer_screening", "eligible", True, "routine", ("elevated", "conflicting_evidence")),
        ("breast_cancer_screening", "eligible", False, "routine", ("routine", "default")),
        ("india_diabetes_screening", "eligible", False, None, ("routine", "default")),  # no floor given
        ("breast_cancer_screening", "eligible", False, "elevated", ("elevated", "guideline_priority")),  # a priority authored on the version is a floor
        ("breast_cancer_screening", "eligible", True, "elevated", ("elevated", "conflicting_evidence")),
        ("breast_cancer_screening", "requires_clinical_review", False, "elevated", ("high", "clinical_review_needed")),
        ("breast_cancer_screening", "eligible", False, "high", ("high", "guideline_priority")),
        ("breast_cancer_screening", "eligible", False, "not_a_level", ("routine", "default")),  # an unknown floor is ignored
    ],
)
def test_derive_priority(activity, outcome, conflicts, floor, expected):
    assert prio.derive_priority(activity, outcome, conflicts, floor) == expected


def test_derivation_is_pure_and_never_lowers():
    for activity in ("brca_risk_assessment", "cervical_cancer_screening"):
        for outcome in ("eligible", "requires_clinical_review"):
            for conflicts in (False, True):
                for floor in prio.PRIORITIES:
                    level, _rule = prio.derive_priority(activity, outcome, conflicts, floor)
                    assert prio.RANK[level] >= prio.RANK[floor]
                    assert (level, _rule) == prio.derive_priority(activity, outcome, conflicts, floor)


# ------------------------------------------------------------------------------------------------ creation
def add_evaluation(db_session, resident, version, outcome="eligible", conflicts=False):
    e = EligibilityEvaluation(
        resident_id=resident.id, guideline_version_id=version.id, outcome=outcome, outcome_class="eligible" if outcome == "eligible" else "indeterminate",
        explanation={}, conflicting_information=[{"detail": "two sources disagree"}] if conflicts else [], input_hash=f"t{outcome}{conflicts}",
        evaluation_date=ON, execution_version="test",
    )  # fmt: skip
    db_session.add(e)
    db_session.flush()
    return e


def make_world(seed, db_session):
    """Cases for one BRCA pathway, one plain programme (eligible, in review, conflicting) created through the real scan."""
    brca, _ = seed.approved_version(care_activity="brca_risk_assessment")
    plain, _ = seed.approved_version(care_activity="plain_screening")
    r = [seed.resident() for _ in range(4)]
    add_evaluation(db_session, r[0], brca)
    add_evaluation(db_session, r[1], plain)
    add_evaluation(db_session, r[2], plain, "requires_clinical_review")
    add_evaluation(db_session, r[3], plain, conflicts=True)
    return [brca, plain], r


def cases_by_resident(db_session, residents):
    rows = db_session.execute(select(CareGapCase).where(CareGapCase.resident_id.in_([r.id for r in residents]))).scalars().all()
    return {c.resident_id: c for c in rows}


def test_cases_are_created_with_their_derived_priority_and_the_rule_is_recorded(seed, seed_module, db_session):
    versions, r = make_world(seed, db_session)
    out = service.create_cases_scan(seed_module.session_factory(db_session), [v.id for v in versions], outcomes=("eligible", "requires_clinical_review"))
    assert out["created"] == 4
    c = cases_by_resident(db_session, r)
    assert [c[x.id].priority for x in r] == ["high", "routine", "high", "elevated"]
    rules = {}
    for x in r:
        first = db_session.execute(select(CaseStatusHistory).where(CaseStatusHistory.case_id == c[x.id].id, CaseStatusHistory.from_status.is_(None))).scalar_one()  # the creation row
        rules[x.id] = (first.details["priority"], first.details["priority_rule"])
    assert [rules[x.id] for x in r] == [("high", "family_history_pathway"), ("routine", "default"), ("high", "clinical_review_needed"), ("elevated", "conflicting_evidence")]


# ------------------------------------------------------------------------------------------------ recompute
def reset_to_routine(db_session):
    db_session.execute(text("UPDATE care_gap_cases SET priority = 'routine'"))
    db_session.flush()


def test_recompute_fixes_open_cases_in_batches_and_leaves_the_rest_alone(seed, seed_module, db_session):
    versions, r = make_world(seed, db_session)
    service.create_cases_scan(seed_module.session_factory(db_session), [v.id for v in versions], outcomes=("eligible", "requires_clinical_review"))
    cases = cases_by_resident(db_session, r)
    done = cases[r[0].id]
    db_session.execute(text("UPDATE care_gap_cases SET status = 'cancelled', closed_at = now() WHERE id = :i"), {"i": done.id})  # a terminal case keeps what it has
    reset_to_routine(db_session)
    stamp = datetime.now() - timedelta(days=9)
    db_session.execute(text("UPDATE care_gap_cases SET updated_at = :t"), {"t": stamp})
    db_session.flush()
    sf = seed_module.session_factory(db_session)
    dry = prio.recompute_open_priorities(sf, chunk_size=2, dry_run=True)
    assert dry["dry_run"] is True and dry["changed"] == 2 and dry["scanned"] == 3  # 3 open cases; BRCA one is cancelled; 2 of the 3 would change
    assert set(db_session.execute(select(CareGapCase.priority)).scalars()) == {"routine"}  # nothing written
    out = prio.recompute_open_priorities(sf, chunk_size=2)  # two batches of 2 and 1
    assert out["scanned"] == 3 and out["changed"] == 2 and out["ruleset"] == prio.RULESET
    assert out["moves"] == {"routine->high": 1, "routine->elevated": 1} and out["distribution_after"] == {"routine": 1, "elevated": 1, "high": 1}
    db_session.expire_all()
    after = cases_by_resident(db_session, r)
    assert [after[x.id].priority for x in r] == ["routine", "routine", "high", "elevated"]  # the cancelled BRCA case was not touched
    assert {c.updated_at.replace(tzinfo=None) for c in after.values()} == {stamp.replace(tzinfo=None)}  # updated_at is not a status change
    events = db_session.execute(select(AuditEvent).where(AuditEvent.action == "caregap.priority_recomputed")).scalars().all()
    assert 1 <= len(events) <= 2 and all(e.details["ruleset"] == prio.RULESET and e.details["adr"] == "ADR-073" for e in events)  # a batch without changes writes none
    assert sum(e.details["changed"] for e in events) == 2 and sum(e.details["cases"] for e in events) == 3
    again = prio.recompute_open_priorities(sf)
    assert again["changed"] == 0 and again["moves"] == {}  # idempotent
    assert db_session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "caregap.priority_recomputed")) == len(events)  # no event for an unchanged run


def test_cli_refuses_beside_case_creating_jobs_and_reports_the_distribution(seed, seed_module, db_session, monkeypatch, capsys):
    import anticipa.db

    versions, _ = make_world(seed, db_session)
    service.create_cases_scan(seed_module.session_factory(db_session), [v.id for v in versions], outcomes=("eligible", "requires_clinical_review"))
    reset_to_routine(db_session)
    monkeypatch.setattr(anticipa.db, "get_sessionmaker", lambda: seed_module.session_factory(db_session))
    db_session.add(ProcessingJob(job_type="guidelines.evaluate", status="running"))
    db_session.flush()
    assert prio.busy_jobs(db_session) == ["guidelines.evaluate"]
    assert prio.main(["recompute"]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["job_types"] == ["guidelines.evaluate"] and "refused" in refused
    assert set(db_session.execute(select(CareGapCase.priority)).scalars()) == {"routine"}
    assert prio.main(["recompute", "--dry-run"]) == 0 and json.loads(capsys.readouterr().out)["changed"] == 3  # a dry run is always allowed
    assert prio.main(["recompute", "--force"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["changed"] == 3 and out["open_cases_by_priority"] == {"high": 2, "elevated": 1, "routine": 1}
    db_session.execute(text("UPDATE processing_jobs SET status = 'completed' WHERE job_type = 'guidelines.evaluate'"))
    db_session.flush()
    assert prio.busy_jobs(db_session) == [] and prio.main(["recompute"]) == 0  # nothing busy: runs without --force (and changes nothing now)
