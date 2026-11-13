"""Population evaluation against the real database (integration, DB `anticipa_test`, rolled back): what every
evaluation
persists (owner requirement), incremental skipping, append-only history, determinism, and the background
job."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from anticipa.guidelines import service
from anticipa.guidelines.handlers import handle
from anticipa.guidelines.models import EligibilityEvaluation
from anticipa.jobs import service as jobs
from anticipa.jobs.context import JobContext

ON = date(2026, 11, 8)


@pytest.fixture
def h(seed_module):
    return seed_module


def evaluations(session, resident=None):
    """Evaluations of one resident, or of the active test dataset (other workstreams may have committed rows
    too)."""  # noqa: E501
    import sys

    from anticipa.datafabric.models import Resident

    stmt = select(EligibilityEvaluation).order_by(
        EligibilityEvaluation.evaluated_at, EligibilityEvaluation.id
    )
    if resident is not None:
        stmt = stmt.where(EligibilityEvaluation.resident_id == resident.id)
    else:
        mine = sys.modules["anticipa_guideline_test_seed"].active_dataset_id()
        stmt = stmt.where(
            EligibilityEvaluation.resident_id.in_(select(Resident.id).where(Resident.dataset_id == mine))
        )
    return list(session.execute(stmt).scalars())


def family_rules(h):
    """Coverage + 'a first-degree relative diagnosed before 60' (synthetic), to exercise conflicts and
    evidence pointers."""  # noqa: E501
    early = h.rule(
        "family_early",
        "inclusion",
        10,
        h.leaf("evidence.synth_family.min_age_at_onset", "LESS_THAN", 60),
        {**h.TRUE_PASSES},
        {"true": None, "false": "not_eligible", "unknown": "insufficient_evidence"},
        "no_early_relative",
        reason_code_when_unknown="relative_age_unresolved",
        source_refs=["synthetic_source#E1"],
    )
    return [h.demo_rules()[0], early]


def test_every_evaluation_persists_what_the_owner_requires(db_session, h, seed):
    version, rules = seed.approved_version()
    young = seed.resident(birth=date(2005, 1, 1))
    eligible = seed.resident(birth=date(1980, 1, 1))
    done = seed.resident(birth=date(1980, 1, 1))
    record = seed.record(done, "procedure", date(2024, 3, 1))
    stats = h.run_evaluation(db_session, [version], ON)
    assert stats.residents == 3 and stats.evaluations_written == 3 and stats.residents_with_errors == 0

    (e_young,) = evaluations(db_session, young)
    assert (e_young.outcome, e_young.outcome_class, e_young.reason_code) == (
        "not_eligible",
        "ineligible",
        "below_minimum_age",
    )
    assert e_young.rule_version_id == rules["age_min"].id  # the decisive rule version (id), not just a name
    assert e_young.guideline_version_id == version.id and e_young.review_status == "pending_clinical_review"
    assert e_young.explanation["guideline"] == {
        "id": str(version.guideline_id),
        "key": e_young.explanation["guideline"]["key"],
        "version": 1,
        "version_id": str(version.id),
        "content_hash": version.content_hash,
    }
    assert e_young.explanation["decisive_rule"] == {
        "rule_id": "demo.age_min",
        "rule_version_id": str(rules["age_min"].id),
        "rule_version": 1,
    }
    assert (
        e_young.explanation["decision"]["explanation"] == "Below the minimum age (synthetic rule)."
    )  # approved template, verbatim
    assert [c["criterion_id"] for c in e_young.explanation["criteria"]] == [
        f"demo.{k}" for k in ("record_coverage", "age_min", "age_max", "excl_condition", "completed_recently")
    ]
    assert {c["criterion_id"]: c["outcome"] for c in e_young.explanation["criteria"]}[
        "demo.age_min"
    ] == "failed"
    ref = e_young.source_references[0]  # citations of the rule behind the decision
    assert ref["rule_id"] == "demo.age_min" and ref["references"][0]["source_id"] == "synthetic_source"
    assert ref["references"][0]["publication_date"] == "2026-01-01" and ref["references"][0][
        "url"
    ].startswith("https://")
    assert ref["metadata"]["source_metadata"]["recommendation_status"] == {"synthetic_source": "synthetic"}
    assert (
        e_young.execution_version.startswith("guideline-engine/")
        and len(e_young.input_hash) == 64
        and e_young.evaluation_date == ON
    )

    (e_ok,) = evaluations(db_session, eligible)
    assert (e_ok.outcome, e_ok.rule_version_id, e_ok.reason_code) == ("eligible", None, None)
    assert e_ok.review_status == "pending_clinical_review"

    (e_done,) = evaluations(db_session, done)
    assert (e_done.outcome, e_done.reason_code) == ("already_completed", "screening_up_to_date")
    pointers = [r for r in e_done.evidence_references if r["type"] == "clinical_record"]
    assert (
        pointers
        and pointers[0]["id"] == str(record.id)
        and pointers[0]["source_reference"] == record.source_reference
    )
    assert e_done.exclusion_reasons == []  # completion is not an exclusion


def test_missing_and_conflicting_information_are_persisted_with_document_pointers(db_session, h, seed):
    version, rules = seed.approved_version(family_rules(h))
    r = seed.resident()
    d1, d2 = seed.document(r), seed.document(r)
    seed.fact(r, relationship="mother", onset=45, quote="Mother diagnosed at 45.", document_id=d1.id)
    seed.fact(r, relationship="mother", onset=62, quote="Mother diagnosed at 62.", document_id=d2.id)
    uncovered = seed.resident(covered=False)
    seed.fact(
        uncovered,
        relationship="father",
        onset=40,
        quote="Father diagnosed at 40.",
        document_id=seed.document(uncovered).id,
    )
    h.run_evaluation(db_session, [version], ON)

    (e,) = evaluations(db_session, r)
    assert (e.outcome, e.outcome_class, e.reason_code) == (
        "insufficient_evidence",
        "indeterminate",
        "relative_age_unresolved",
    )
    (conflict,) = e.conflicting_information
    assert conflict["type"] == "conflicting_age_at_onset" and conflict["values"] == [45, 62]
    assert {s["document_id"] for s in conflict["sources"]} == {str(d1.id), str(d2.id)}
    assert {s["quote"] for s in conflict["sources"]} == {"Mother diagnosed at 45.", "Mother diagnosed at 62."}
    pointers = {
        p.get("document_id"): p.get("quote") for p in e.evidence_references if p["type"] == "evidence_fact"
    }
    assert pointers == {str(d1.id): "Mother diagnosed at 45.", str(d2.id): "Mother diagnosed at 62."}
    assert [m["criterion_id"] for m in e.missing_information] == ["demo.family_early"]

    (e2,) = evaluations(db_session, uncovered)
    assert e2.outcome == "insufficient_evidence" and e2.reason_code == "no_clinical_record_coverage"
    assert (
        e2.missing_information[0]["criterion_id"] == "demo.record_coverage"
        and e2.conflicting_information == []
    )


def test_unaccepted_evidence_never_reaches_a_decision(db_session, h, seed):
    version, _ = seed.approved_version(family_rules(h))
    r = seed.resident()
    seed.fact(r, relationship="mother", onset=45, validation="requires_review")
    seed.fact(r, relationship="father", onset=40, validation="rejected")
    h.run_evaluation(db_session, [version], ON)
    (e,) = evaluations(db_session, r)
    assert (
        e.outcome == "insufficient_evidence"
    )  # the unreviewed fact is an unresolved candidate; the rejected one does not exist
    assert (
        len([p for p in e.evidence_references if p["type"] == "evidence_fact"]) == 1
    )  # only the unreviewed candidate is referenced


def test_clinical_review_attestation_is_copied_onto_evaluations(db_session, h, seed):
    version, _ = seed.approved_version(clinically_reviewed=True)
    r = seed.resident()
    h.run_evaluation(db_session, [version], ON)
    assert evaluations(db_session, r)[0].review_status == "clinically_reviewed"


def test_sex_assigned_at_birth_is_populated_from_administrative_sex(db_session, h, seed):
    sex_rule = h.rule(
        "sex",
        "inclusion",
        12,
        h.leaf("resident.sex_assigned_at_birth", "EQUALS", "female"),
        {**h.TRUE_PASSES},
        {"true": None, "false": "not_eligible", "unknown": "insufficient_evidence"},
        "wrong_sex",
    )
    version, _ = seed.approved_version([h.demo_rules()[0], sex_rule])
    f, m, u = seed.resident(sex="female"), seed.resident(sex="male"), seed.resident(sex="unknown")
    h.run_evaluation(db_session, [version], ON)
    assert [evaluations(db_session, x)[0].outcome for x in (f, m, u)] == [
        "eligible",
        "not_eligible",
        "insufficient_evidence",
    ]


def test_incremental_evaluation_skips_unchanged_inputs_and_keeps_history(db_session, h, seed):
    version, _ = seed.approved_version()
    a, b = seed.resident(), seed.resident()
    first = h.run_evaluation(db_session, [version], ON)
    assert (first.evaluations_written, first.skipped_unchanged) == (2, 0)
    second = h.run_evaluation(db_session, [version], ON)
    assert (second.evaluations_written, second.skipped_unchanged) == (
        0,
        2,
    )  # nothing relevant changed: no work, no new rows
    assert len(evaluations(db_session)) == 2
    seed.record(a, "procedure", date(2025, 6, 1))  # new relevant evidence for resident a only
    third = h.run_evaluation(db_session, [version], ON)
    assert (third.evaluations_written, third.skipped_unchanged) == (1, 1)
    history = evaluations(db_session, a)
    assert [e.outcome for e in history] == [
        "eligible",
        "already_completed",
    ]  # the earlier decision is kept, never overwritten
    assert len(evaluations(db_session, b)) == 1
    noise = seed.record(b, "procedure", date(2025, 6, 1), c="UNRELATED-CODE")  # irrelevant to every rule
    assert noise is not None and h.run_evaluation(db_session, [version], ON).evaluations_written == 0
    assert (
        h.run_evaluation(db_session, [version], date(2026, 11, 9)).evaluations_written == 2
    )  # a different evaluation date re-evaluates
    forced = h.run_evaluation(db_session, [version], date(2026, 11, 9), mode="full")
    assert (
        forced.evaluations_written == 2 and forced.skipped_unchanged == 0
    )  # an explicit full re-evaluation writes again


def test_evaluations_are_append_only_in_the_database(db_session, h, seed):
    version, _ = seed.approved_version()
    r = seed.resident()
    h.run_evaluation(db_session, [version], ON)
    (e,) = evaluations(db_session, r)
    for sql in (
        "UPDATE eligibility_evaluations SET outcome = 'eligible' WHERE id = :id",
        "UPDATE eligibility_evaluations SET explanation = '{}'::jsonb WHERE id = :id",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            with db_session.begin_nested():
                db_session.execute(text(sql), {"id": e.id})


def test_historical_evaluations_stay_unchanged_after_a_new_version_is_approved(db_session, h, seed):
    v1, _ = seed.approved_version()
    r = seed.resident(birth=date(1980, 1, 1))
    h.run_evaluation(db_session, [v1], ON)
    before = json.dumps(
        [
            (str(e.id), e.outcome, e.explanation, e.input_hash, e.guideline_version_id)
            for e in evaluations(db_session, r)
        ],
        sort_keys=True,
        default=str,
    )
    from anticipa.guidelines.models import Guideline

    g = db_session.get(Guideline, v1.guideline_id)
    stricter = h.demo_rules()
    stricter[1]["expression"]["value"] = (
        50  # v2: minimum age 50 -> the same resident (46) is no longer eligible
    )
    v2, _ = seed.approved_version(stricter, guideline=g)
    service.retire(db_session, h.REVIEWER, v1, "superseded")
    h.run_evaluation(db_session, [v2], ON)
    after_old = json.dumps(
        [
            (str(e.id), e.outcome, e.explanation, e.input_hash, e.guideline_version_id)
            for e in evaluations(db_session, r)
            if e.guideline_version_id == v1.id
        ],
        sort_keys=True,
        default=str,
    )
    assert after_old == before  # the old decision is untouched and still points at the old version
    assert [(e.guideline_version_id, e.outcome) for e in evaluations(db_session, r)][-1] == (
        v2.id,
        "not_eligible",
    )


def test_same_inputs_give_identical_evaluations_across_runs(db_session, h, seed):
    version, _ = seed.approved_version(family_rules(h))
    r = seed.resident()
    seed.fact(r, relationship="mother", onset=50, quote="q", document_id=seed.document(r).id)
    h.run_evaluation(db_session, [version], ON)
    h.run_evaluation(db_session, [version], ON, mode="full")
    first, second = evaluations(db_session, r)
    assert first.id != second.id and first.input_hash == second.input_hash
    for col in (
        "outcome",
        "outcome_class",
        "reason_code",
        "explanation",
        "evidence_references",
        "missing_information",
        "conflicting_information",
        "source_references",
        "exclusion_reasons",
        "review_status",
    ):
        assert getattr(first, col) == getattr(second, col), col


def test_only_approved_effective_versions_can_be_evaluated(db_session, h, seed):
    from anticipa.core.errors import Conflict

    draft, _ = seed.approved_version(approve=False)
    seed.resident()
    with pytest.raises(Conflict):
        h.run_evaluation(db_session, [draft], ON)
    assert evaluations(db_session) == []


def test_chunking_and_resume_cover_every_resident_exactly_once(db_session, h, seed):
    from anticipa.guidelines.population import EvalScope, evaluate_population

    version, _ = seed.approved_version()
    residents = sorted((seed.resident() for _ in range(7)), key=lambda r: r.id)
    factory = h.session_factory(db_session)
    chunks = []
    ds = seed.dataset.dataset_version
    stats = evaluate_population(
        factory,
        EvalScope(ON, [version.id], dataset_version=ds, chunk_size=3),
        on_chunk=lambda c, t: chunks.append((c.residents, t.residents)),
    )
    assert (stats.residents, stats.evaluations_written) == (7, 7) and chunks == [(3, 3), (3, 6), (1, 7)]
    assert len({e.resident_id for e in evaluations(db_session)}) == 7
    # a run that stopped after two chunks resumes from its cursor: nothing is evaluated twice
    from anticipa.guidelines.population import resolve_guidelines  # noqa: F401

    db_session.execute(
        text(
            "DELETE FROM eligibility_evaluations WHERE resident_id IN (SELECT id FROM residents WHERE dataset_id = :d)"  # noqa: E501
        ),
        {"d": seed.dataset.id},
    )
    cursor = residents[2].id
    resumed = evaluate_population(
        factory, EvalScope(ON, [version.id], dataset_version=ds, chunk_size=3), resume_after=cursor
    )
    assert resumed.residents == 4 and {e.resident_id for e in evaluations(db_session)} == {
        r.id for r in residents[3:]
    }


def test_the_evaluation_job_end_to_end(db_session, h, seed):
    version, _ = seed.approved_version()
    residents = [seed.resident() for _ in range(5)]
    scope = {
        "evaluation_date": ON.isoformat(),
        "guideline_version_ids": [str(version.id)],
        "mode": "incremental",
        "chunk_size": 2,
        "resident_ids": [str(r.id) for r in residents],
    }
    result = jobs.submit_job(db_session, "guidelines.evaluate_population", scope, "idem-key-1", h.REVIEWER.id)
    again = jobs.submit_job(db_session, "guidelines.evaluate_population", scope, "idem-key-1", h.REVIEWER.id)
    assert result.created and not again.created and again.job.id == result.job.id  # idempotent submission
    claimed = (
        result.job
    )  # claim our own job explicitly: the shared test database may hold other workstreams' queued jobs
    claimed.status, claimed.locked_by = "running", "test-worker"
    db_session.flush()
    ctx = JobContext(h.session_factory(db_session), claimed, "test-worker")
    handle(ctx)
    assert jobs.finish_success(db_session, claimed.id, "test-worker") == "completed"
    job = jobs.get_job(db_session, claimed.id)
    db_session.refresh(job)
    assert (job.total_items, job.processed_items, job.succeeded_items, job.failed_items) == (5, 5, 5, 0)
    assert job.checkpoint["last_resident_id"] and job.checkpoint["totals"]["evaluations_written"] == 5
    assert len(evaluations(db_session)) == 5 and {e.job_id for e in evaluations(db_session)} == {claimed.id}
    audit_rows = db_session.execute(
        text(
            "SELECT details FROM audit_events WHERE action = 'eligibility.evaluate_population' AND resource_id = :j"  # noqa: E501
        ),
        {"j": str(claimed.id)},
    ).all()
    assert audit_rows and audit_rows[0][0]["evaluations_written"] == 5


def test_a_rule_execution_error_is_a_recorded_failed_item_not_a_crash(db_session, h, seed):
    version, _ = seed.approved_version()
    bad = seed.resident(birth=date(2030, 1, 1))  # born after the evaluation date: the engine reports an error
    good = seed.resident()
    stats = h.run_evaluation(db_session, [version], ON)
    assert stats.residents_with_errors == 1 and stats.outcomes["evaluation_error"] == 1
    assert (
        evaluations(db_session, bad)[0].outcome_class == "error"
        and evaluations(db_session, good)[0].outcome == "eligible"
    )


def test_submit_queues_every_approved_version_resumably_and_idempotently(db_session, h, seed):
    from anticipa.guidelines.handlers import submit_evaluation_job

    version, _ = seed.approved_version()
    seed.approved_version(approve=False)  # a draft must never be queued
    first = submit_evaluation_job(
        db_session,
        evaluation_date=ON,
        dataset_version=seed.dataset.dataset_version,
        idempotency_key="submit-key-1",
    )
    scope = first.job.input_scope
    assert (
        first.created
        and first.job.status == "queued"
        and first.job.job_type == "guidelines.evaluate_population"
    )
    assert (
        str(version.id) in scope["guideline_version_ids"]
        and scope["mode"] == "incremental"
        and scope["create_cases"] is True
    )
    assert (
        scope["evaluation_date"] == ON.isoformat()
        and scope["dataset_version"] == seed.dataset.dataset_version
    )
    again = submit_evaluation_job(db_session, evaluation_date=ON, idempotency_key="submit-key-1")
    assert not again.created and again.job.id == first.job.id
    audit = db_session.execute(
        text(
            "SELECT count(*) FROM audit_events WHERE action = 'eligibility.job_submit' AND resource_id = :j"
        ),
        {"j": str(first.job.id)},
    ).scalar_one()
    assert audit == 1


def test_scope_by_storage_profile_and_guideline_keys(db_session, h, seed):
    """Lean (`;profile=rules`) residents lack records some guidelines need, so a run can be limited to the
    full profile; and a run can be limited to named guidelines (an unapproved key is an error)."""
    import uuid

    from anticipa.core.errors import Conflict, ValidationFailed
    from anticipa.guidelines.handlers import scope_from_input, submit_evaluation_job
    from anticipa.guidelines.population import EvalScope, evaluate_population, resolve_guidelines

    key_a, key_b = f"test-scope-a-{uuid.uuid4().hex[:6]}", f"test-scope-b-{uuid.uuid4().hex[:6]}"
    va, _ = seed.approved_version(guideline_key=key_a)
    vb, _ = seed.approved_version(guideline_key=key_b)
    draft_key = f"test-scope-d-{uuid.uuid4().hex[:6]}"
    seed.approved_version(guideline_key=draft_key, approve=False)
    full = [seed.resident() for _ in range(3)]
    lean = [seed.resident() for _ in range(2)]
    for r in full:
        r.source_version = "synthea-4.0.0"
    for r in lean:
        r.source_version = "synthea-4.0.0;profile=rules"
    db_session.flush()
    factory, ds = h.session_factory(db_session), seed.dataset.dataset_version

    def run(profile):
        return evaluate_population(
            factory, EvalScope(ON, [va.id], dataset_version=ds, storage_profile=profile)
        )

    assert run("full").residents == 3
    assert {e.resident_id for e in evaluations(db_session)} == {
        r.id for r in full
    }  # lean residents untouched
    assert run("rules").residents == 2
    assert run(None).residents == 5
    with pytest.raises(ValidationFailed):
        run("everything")

    only_a = resolve_guidelines(db_session, ON, guideline_keys=[key_a])
    assert [g.guideline_key for g in only_a] == [key_a]
    assert {g.guideline_key for g in resolve_guidelines(db_session, ON, guideline_keys=[key_a, key_b])} == {
        key_a,
        key_b,
    }
    with pytest.raises(Conflict):  # a draft has no approved version; a missing key is not silently dropped
        resolve_guidelines(db_session, ON, guideline_keys=[key_a, draft_key])
    with pytest.raises(Conflict):
        resolve_guidelines(db_session, ON, guideline_keys=["no-such-guideline"])

    queued = submit_evaluation_job(
        db_session,
        evaluation_date=ON,
        dataset_version=ds,
        guideline_keys=[key_a],
        storage_profile="full",
        idempotency_key=f"scope-{key_a}",
    )
    scope = queued.job.input_scope
    assert scope["guideline_version_ids"] == [str(va.id)] and scope["storage_profile"] == "full"
    assert scope_from_input(scope).storage_profile == "full" and scope_from_input({}).storage_profile is None
    with pytest.raises(ValidationFailed):
        submit_evaluation_job(db_session, evaluation_date=ON, storage_profile="lean")
