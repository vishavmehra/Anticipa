"""Guideline workflow, immutability (service AND database), draft import, version selection (integration: DB
`anticipa_test`, every test rolled back)."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from anticipa.core.errors import Conflict, InvalidTransition, ValidationFailed
from anticipa.guidelines import service
from anticipa.guidelines.models import Guideline, GuidelineVersion, RuleVersion
from anticipa.guidelines.population import resolve_guidelines

ON = date(2026, 11, 8)


@pytest.fixture
def helpers(seed_module):
    return seed_module


def db_rejects(session, sql: str, **params) -> str:
    with pytest.raises(DBAPIError) as exc:
        with session.begin_nested():
            session.execute(text(sql), params)
    return str(exc.value.orig)


# ---------------------------------------------------------------------------------------------- workflow +
# immutability
def test_draft_review_approve_retire_and_immutability(db_session, helpers, seed):
    reviewer = helpers.REVIEWER
    v, rules = seed.approved_version(approve=False)
    assert (
        v.review_status == "draft"
        and v.version == 1
        and v.content_hash
        and v.clinical_review_status == "pending_clinical_review"
    )
    draft_hash = v.content_hash

    service.submit_for_review(db_session, reviewer, v)
    assert v.review_status == "in_review"
    with pytest.raises(InvalidTransition):  # no edits while in review
        service.add_rule(db_session, reviewer, v, helpers.demo_rules()[1] | {"rule_id": "demo.extra"})
    service.approve(db_session, reviewer, v)
    assert v.review_status == "approved" and v.approved_by == reviewer.id and v.approved_at is not None
    assert v.content_hash == draft_hash  # frozen content hash
    assert (
        v.clinical_review_status == "pending_clinical_review"
    )  # approved for use is not clinically reviewed
    rule = service.rules_of(db_session, v.id)[0]
    assert rule.approval_status == "approved"

    # service layer
    with pytest.raises(InvalidTransition):
        service.add_rule(db_session, reviewer, v, helpers.demo_rules()[1] | {"rule_id": "demo.extra"})
    with pytest.raises(InvalidTransition):
        service.delete_rule(db_session, reviewer, rule)
    # database layer: the triggers hold even when the service is bypassed
    assert "immutable" in db_rejects(
        db_session, "UPDATE rule_versions SET rule_definition = '{}'::jsonb WHERE id = :id", id=rule.id
    )
    assert "immutable" in db_rejects(
        db_session, "UPDATE rule_versions SET name = 'x' WHERE id = :id", id=rule.id
    )
    assert "immutable" in db_rejects(
        db_session, "UPDATE guideline_versions SET source_reference = 'x' WHERE id = :id", id=v.id
    )
    assert "immutable" in db_rejects(
        db_session, "UPDATE guideline_versions SET selectors = '{}'::jsonb WHERE id = :id", id=v.id
    )
    assert "immutable" in db_rejects(
        db_session,
        "UPDATE guideline_versions SET clinical_review_status = 'clinically_reviewed' WHERE id = :id",
        id=v.id,
    )
    assert "immutable" in db_rejects(
        db_session, "UPDATE guideline_versions SET effective_from = '2000-01-01' WHERE id = :id", id=v.id
    )
    assert "cannot be deleted" in db_rejects(
        db_session, "DELETE FROM rule_versions WHERE id = :id", id=rule.id
    )
    assert "cannot be deleted" in db_rejects(
        db_session, "DELETE FROM guideline_versions WHERE id = :id", id=v.id
    )
    assert "draft guideline version" in db_rejects(
        db_session,
        "INSERT INTO rule_versions (guideline_version_id, rule_key, rule_id, rule_type, precedence, name, care_activity, rule_definition, content_hash) "  # noqa: E501
        "VALUES (:v, 'sneaky', 'x.sneaky', 'inclusion', 1, 'sneaky', 'x', '{}'::jsonb, 'h')",
        v=v.id,
    )

    service.retire(db_session, reviewer, v, "superseded in test")
    assert v.review_status == "retired" and v.retirement_reason == "superseded in test"
    assert "immutable" in db_rejects(
        db_session, "UPDATE guideline_versions SET retirement_reason = 'x' WHERE id = :id", id=v.id
    )
    assert "immutable" in db_rejects(
        db_session, "UPDATE rule_versions SET approval_status = 'approved' WHERE id = :id", id=rule.id
    )
    with pytest.raises(InvalidTransition):
        service.retire(db_session, reviewer, v, None)


def test_approval_can_carry_an_explicit_clinical_review_attestation(db_session, helpers, seed):
    v, _ = seed.approved_version(clinically_reviewed=True)
    assert v.clinical_review_status == "clinically_reviewed"


def test_invalid_transitions_are_rejected(db_session, helpers, seed):
    reviewer = helpers.REVIEWER
    draft, _ = seed.approved_version(approve=False)
    for action in (
        lambda: service.approve(db_session, reviewer, draft),  # draft cannot jump to approved
        lambda: service.retire(db_session, reviewer, draft, None),
        lambda: service.return_to_draft(db_session, reviewer, draft, None),
    ):
        with pytest.raises(InvalidTransition):
            action()
    service.submit_for_review(db_session, reviewer, draft)
    with pytest.raises(InvalidTransition):
        service.submit_for_review(db_session, reviewer, draft)
    service.return_to_draft(db_session, reviewer, draft, "needs changes")
    assert draft.review_status == "draft"
    assert all(r.approval_status == "draft" for r in service.rules_of(db_session, draft.id))
    extra = helpers.demo_rules()[1] | {"rule_id": "demo.extra_rule", "priority": 12}
    service.add_rule(db_session, reviewer, draft, extra)  # a returned draft is editable again
    assert len(service.rules_of(db_session, draft.id)) == 6
    rule = next(r for r in service.rules_of(db_session, draft.id) if r.rule_key == "extra_rule")
    service.delete_rule(db_session, reviewer, rule)
    assert len(service.rules_of(db_session, draft.id)) == 5


def test_submit_requires_source_scope_and_rules(db_session, helpers):
    reviewer = helpers.REVIEWER
    g = service.create_guideline(
        db_session, reviewer, guideline_key=f"inc-{uuid.uuid4().hex[:8]}", name="Incomplete"
    )
    v = service.create_version(db_session, reviewer, g, effective_from=date(2020, 1, 1))
    with pytest.raises(ValidationFailed) as exc:
        service.submit_for_review(db_session, reviewer, v)
    assert {"rules", "source_reference", "population_scope"} <= {d["field"] for d in exc.value.details}


def test_invalid_definitions_are_rejected_with_details_and_create_nothing(db_session, helpers):
    reviewer = helpers.REVIEWER
    g = service.create_guideline(
        db_session, reviewer, guideline_key=f"bad-{uuid.uuid4().hex[:8]}", name="Bad"
    )
    bad_rule = helpers.demo_rules()[1]
    bad_rule["expression"] = {"field": "resident.income", "operator": "EQUALS", "value": 1}
    with pytest.raises(ValidationFailed) as exc:
        service.create_version(
            db_session,
            reviewer,
            g,
            effective_from=date(2020, 1, 1),
            selectors=helpers.selectors(),
            rules=[bad_rule],
        )
    assert exc.value.code == "VALIDATION_ERROR" and exc.value.details
    assert (
        db_session.execute(select(GuidelineVersion).where(GuidelineVersion.guideline_id == g.id)).first()
        is None
    )
    missing_selector = helpers.demo_rules()
    with pytest.raises(ValidationFailed) as exc2:
        service.create_version(
            db_session, reviewer, g, effective_from=date(2020, 1, 1), selectors={}, rules=missing_selector
        )
    assert any("unknown selectors" in d["reason"] for d in exc2.value.details)


def test_add_rule_validates_and_rejects_duplicates(db_session, helpers, seed):
    v, _ = seed.approved_version(approve=False)
    dup = helpers.demo_rules()[1]
    with pytest.raises(Conflict):
        service.add_rule(db_session, helpers.REVIEWER, v, dup)
    broken = helpers.demo_rules()[1] | {
        "rule_id": "demo.broken",
        "outcome_by_expression_result": {"true": "approved_anyway", "false": None, "unknown": None},
    }
    with pytest.raises(ValidationFailed):
        service.add_rule(db_session, helpers.REVIEWER, v, broken)


def test_approval_detects_content_changed_after_submission(db_session, helpers, seed):
    v, _ = seed.approved_version(approve=False)
    service.submit_for_review(db_session, helpers.REVIEWER, v)
    db_session.execute(
        text("UPDATE rule_versions SET content_hash = 'tampered' WHERE guideline_version_id = :v"),
        {"v": v.id},
    )
    with pytest.raises(Conflict):
        service.approve(db_session, helpers.REVIEWER, v)
    assert v.review_status == "in_review"


def test_version_and_rule_numbering(db_session, helpers, seed):
    v1, r1 = seed.approved_version()
    g = db_session.get(Guideline, v1.guideline_id)
    v2, r2 = seed.approved_version(guideline=g, approve=False)
    assert (v1.version, v2.version) == (1, 2)
    assert (r1["age_min"].version, r2["age_min"].version) == (1, 2)
    stored = r1["age_min"]
    assert stored.rule_id == "demo.age_min" and stored.rule_type == "inclusion" and stored.precedence == 10
    assert stored.care_activity == v1.care_activity and stored.rule_language == "anticipa-08-v1"


# ---------------------------------------------------------------------------------------------- import of
# drafts
def _draft_file(helpers, *, key="import-test", rules=None):
    return {
        "selector_library": {**helpers.selectors(), "unused_selector": helpers.selectors()["synth_done"]},
        "guidelines": [
            {
                "guideline_key": key,
                "name": "Import test",
                "clinical_domain": "test",
                "source_organization": "Synthetic fixture",
                "source_reference": "synthetic_source",
                "effective_from": "2020-01-01",
                "status": "draft",
                "label": "illustrative - pending clinical review",
                "population_scope": "Synthetic residents.",
                "version": {
                    "version_number": "0.1.0-draft",
                    "publication_date": "2026-01-01",
                    "approval_status": "draft",
                },
                "sources": helpers.demo_sources(),
                "decision_policy": None,
                "rules": rules or helpers.demo_rules(),
                "test_cases": [{"id": "ignored"}],
            }
        ],
    }


def test_import_creates_drafts_only_and_is_idempotent(db_session, helpers):
    payload = _draft_file(helpers)
    payload["guidelines"][0]["status"] = "approved"  # a file cannot approve anything
    first = service.import_drafts(db_session, payload)
    assert first.errors == [] and (first.guidelines_created, first.versions_created, first.rules_created) == (
        1,
        1,
        5,
    )
    assert any("drafts only" in w for w in first.warnings)
    (version,) = db_session.execute(
        select(GuidelineVersion).join(Guideline).where(Guideline.guideline_key == "import-test")
    ).scalars()
    assert version.review_status == "draft" and version.clinical_review_status == "pending_clinical_review"
    assert version.care_activity == "import_test" and version.source_version == "0.1.0-draft"
    assert set(version.selectors) == {
        "synth_coverage",
        "synth_done",
        "synth_excluding_condition",
    }  # only what the rules reference
    assert (
        version.disclaimer == "illustrative - pending clinical review"
        and version.sources[0]["source_id"] == "synthetic_source"
    )
    assert {r.approval_status for r in service.rules_of(db_session, version.id)} == {"draft"}
    again = service.import_drafts(db_session, payload)
    assert (again.versions_created, again.skipped_unchanged) == (0, 1)
    changed = _draft_file(helpers)
    changed["guidelines"][0]["rules"][1]["expression"]["value"] = 41
    third = service.import_drafts(db_session, changed)
    assert (third.guidelines_created, third.versions_created) == (0, 1)  # new content -> next draft version
    assert sorted(
        db_session.execute(
            select(GuidelineVersion.version).join(Guideline).where(Guideline.guideline_key == "import-test")
        ).scalars()
    ) == [1, 2]


def test_import_rejects_invalid_rules_without_importing_anything(db_session, helpers):
    rules = helpers.demo_rules()
    rules[1]["expression"]["operator"] = "CONTAINS"
    report = service.import_drafts(db_session, _draft_file(helpers, rules=rules))
    assert report.errors and report.errors[0]["guideline"] == "import-test" and report.guidelines_created == 0
    assert (
        db_session.execute(
            text("SELECT count(*) FROM guidelines WHERE guideline_key = 'import-test'")
        ).scalar_one()
        == 0
    )


def test_import_dry_run_writes_nothing(db_session, helpers):
    report = service.import_drafts(db_session, _draft_file(helpers), dry_run=True)
    assert (report.guidelines_created, report.versions_created, report.rules_created) == (1, 1, 5)
    assert (
        db_session.execute(
            text("SELECT count(*) FROM guidelines WHERE guideline_key = 'import-test'")
        ).scalar_one()
        == 0
    )


def test_the_shipped_research_drafts_import_as_drafts_and_validate(db_session, helpers):
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "config" / "guidelines" / "rules_draft.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    report = service.import_drafts(db_session, copy.deepcopy(payload))
    assert report.errors == [], report.errors
    assert report.guidelines_created == len(payload["guidelines"]) and report.rules_created == sum(
        len(g["rules"]) for g in payload["guidelines"]
    )
    statuses = set(db_session.execute(select(GuidelineVersion.review_status)).scalars())
    assert statuses == {"draft"}
    assert service.import_drafts(db_session, copy.deepcopy(payload)).skipped_unchanged == len(
        payload["guidelines"]
    )  # idempotent
    # drafts are never evaluated: nothing is approved
    assert resolve_guidelines(db_session, ON) == []


# ---------------------------------------------------------------------------------------------- selection
def test_only_approved_effective_versions_are_selected(db_session, helpers, seed):
    approved, _ = seed.approved_version()
    draft, dr = seed.approved_version(approve=False)
    future, _ = seed.approved_version(effective_from=date(2030, 1, 1))
    expired, er = seed.approved_version(effective_until=date(2021, 1, 1))
    retired, _ = seed.approved_version()
    service.retire(db_session, helpers.REVIEWER, retired, "test")

    selected = {g.guideline_version_id for g in resolve_guidelines(db_session, ON)}
    assert approved.id in selected and not selected & {draft.id, future.id, expired.id, retired.id}
    # a historical evaluation uses an evaluation date inside the window, not a retired/expired version by fiat
    assert [
        g.guideline_version_id
        for g in resolve_guidelines(db_session, date(2020, 6, 1), guideline_version_ids=[expired.id])
    ] == [expired.id]
    # explicit ids that do not qualify are an error, never silently dropped (08 §16.4 #8)
    for bad in (draft, future, retired, expired):
        with pytest.raises(Conflict):
            resolve_guidelines(db_session, ON, guideline_version_ids=[bad.id])
    with pytest.raises(Conflict):
        resolve_guidelines(db_session, ON, rule_version_ids=[dr["age_min"].id])
    assert [
        g.guideline_version_id
        for g in resolve_guidelines(
            db_session, ON, rule_version_ids=[service.rules_of(db_session, approved.id)[0].id]
        )
    ] == [approved.id]
    assert {r.rule_id for r in service.rules_of(db_session, expired.id)} and er


def test_highest_effective_version_per_guideline_wins(db_session, helpers, seed):
    v1, _ = seed.approved_version()
    g = db_session.get(Guideline, v1.guideline_id)
    v2, _ = seed.approved_version(guideline=g)
    selected = [x for x in resolve_guidelines(db_session, ON) if x.guideline_version_id in (v1.id, v2.id)]
    assert [x.guideline_version_id for x in selected] == [v2.id]


def test_rule_versions_store_the_whole_rule_for_traceability(db_session, helpers, seed):
    v, rules = seed.approved_version()
    stored = db_session.execute(
        select(RuleVersion).where(RuleVersion.guideline_version_id == v.id, RuleVersion.rule_key == "age_min")
    ).scalar_one()
    assert stored.rule_definition["source_refs"] == [
        "synthetic_source#E1"
    ]  # metadata kept verbatim, never executed
    assert stored.rule_definition["criterion_semantics"] == {
        "true": "passed",
        "false": "failed",
        "unknown": "unknown",
    }
    assert rules["age_min"].id == stored.id
