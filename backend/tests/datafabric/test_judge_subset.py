# ruff: noqa: E501, S608
"""Judge subset export/import (anticipa.datafabric.judge_subset): selection, replace-on-rerun, safety refusals.

Source = `anticipa_test` (db_session), target = `anticipa_test_ddl` (ddl_session; the tool disables the immutability
triggers, which takes table locks). Both sessions are rolled back at the end; the tool runs inside them through
savepoints. Document text goes through two LocalDocumentStores in tmp_path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from anticipa.datafabric import judge_subset as js
from anticipa.datafabric.docstore import LocalDocumentStore, content_hash

SRC_VERSION = "jst-src-v1"
JUDGE = f"{SRC_VERSION}-judge"
FAMILIES = {"A": 4, "B": 3, "C": 2}  # plus singletons S0..S5
COHORT = {"A0", "B1", "C0", "S0"}  # residents with evidence facts
WITH_DOCS = {"A0", "A1", "B1", "B2", "C0", "S0", "S1", "S2", "S3"}


@dataclass
class Source:
    dataset_id: uuid.UUID
    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    docs: dict[str, tuple[uuid.UUID, str]] = field(default_factory=dict)  # label -> (document id, source key)
    version_id: uuid.UUID | None = None


def raw(session: Session):
    return session.connection().connection.driver_connection


def q(session: Session, sql: str, **params):
    return session.execute(text(sql), params)


def n(session: Session, table: str, where: str = "TRUE", **params) -> int:
    return q(session, f"SELECT count(*) FROM anticipa.{table} WHERE {where}", **params).scalar_one()


def build_source(s: Session, store: LocalDocumentStore) -> Source:
    ds = q(
        s, "INSERT INTO anticipa.datasets (dataset_version, generator, base_seed) VALUES (:v, 'test', 7) RETURNING id", v=SRC_VERSION
    ).scalar_one()
    src = Source(ds)
    labels = [f"{f}{i}" for f, size in FAMILIES.items() for i in range(size)] + [f"S{i}" for i in range(6)]
    for label in labels:
        rid = uuid.uuid4()
        src.ids[label] = rid
        q(s, "INSERT INTO anticipa.residents (id, dataset_id, source_system, source_patient_id) VALUES (:i, :d, 'test', :p)", i=rid, d=ds, p=label)
        for k in range(2):
            q(
                s,
                "INSERT INTO anticipa.clinical_records (resident_id, resource_type, source_resource_id, category, source_reference) "
                "VALUES (:r, 'Condition', :c, 'condition', 'Condition/x')",
                r=rid, c=f"{label}-{k}",
            )
    for fam, size in FAMILIES.items():  # a star: member 0 is related to every other member
        for i in range(1, size):
            q(
                s,
                "INSERT INTO anticipa.family_edges (resident_id, related_resident_id, relationship_type, relationship_direction, source_type) "
                "VALUES (:a, :b, 'sibling', 'related_to_resident', 'deterministic_linker')",
                a=src.ids[f"{fam}0"], b=src.ids[f"{fam}{i}"],
            )
    for label in sorted(WITH_DOCS):
        add_document(s, store, src, label, f"clinical note of {label}")

    # approved guideline version + rule (inserted as draft, approved afterwards: the guards demand that order)
    g = q(s, "INSERT INTO anticipa.guidelines (guideline_key, name) VALUES ('jst-guideline', 'Test guideline') RETURNING id").scalar_one()
    src.version_id = q(
        s,
        "INSERT INTO anticipa.guideline_versions (guideline_id, version, effective_from, care_activity) "
        "VALUES (:g, 1, :d, 'jst_activity') RETURNING id",
        g=g, d=date(2026, 1, 1),
    ).scalar_one()
    q(
        s,
        "INSERT INTO anticipa.rule_versions (guideline_version_id, rule_key, name, care_activity, rule_definition, content_hash, rule_id, rule_type, precedence) "
        "VALUES (:v, 'r1', 'rule', 'jst_activity', '{}', 'h', 'r1', 'inclusion', 0)",
        v=src.version_id,
    )
    q(s, "UPDATE anticipa.guideline_versions SET review_status = 'approved' WHERE id = :v", v=src.version_id)
    q(s, "UPDATE anticipa.rule_versions SET approval_status = 'approved' WHERE guideline_version_id = :v", v=src.version_id)

    for label in COHORT:  # AI cohort: one invocation, one fact, one verified source each
        inv, fact = uuid.uuid4(), uuid.uuid4()
        q(s, "INSERT INTO anticipa.model_invocations (id, task, provider, model_id, status, job_id) VALUES (:i, 'evidence.extract', 'anthropic', 'm', 'succeeded', NULL)", i=inv)
        # a family-member fact points at a relative inside the family; one fact points outside (must be nulled)
        outside = src.ids["S5"] if label == "S0" else None
        inside = src.ids["A1"] if label == "A0" else None
        q(
            s,
            "INSERT INTO anticipa.evidence_facts (id, resident_id, category, fact_type, assertion_status, extraction_method, invocation_id, subject_person_id) "
            "VALUES (:f, :r, 'family_history', 'condition', 'positive', 'llm', :i, :sp)",
            f=fact, r=src.ids[label], i=inv, sp=inside or outside,
        )
        if label in WITH_DOCS:
            q(
                s,
                "INSERT INTO anticipa.evidence_sources (evidence_fact_id, verification_status, document_id, source_quote) "
                "VALUES (:f, 'verified_exact', :d, 'clinical note')",
                f=fact, d=src.docs[label][0],
            )
    ev = q(
        s,
        "INSERT INTO anticipa.eligibility_evaluations (resident_id, guideline_version_id, outcome, outcome_class, explanation, input_hash, evaluation_date, execution_version, job_id) "
        "VALUES (:r, :v, 'eligible', 'eligible', '{}', 'h', :d, 'v1', NULL) RETURNING id",
        r=src.ids["A0"], v=src.version_id, d=date(2026, 11, 8),
    ).scalar_one()
    case = q(
        s,
        "INSERT INTO anticipa.care_gap_cases (resident_id, evaluation_id, guideline_version_id, care_activity, source_outcome) "
        "VALUES (:r, :e, :v, 'jst_activity', 'eligible') RETURNING id",
        r=src.ids["A0"], e=ev, v=src.version_id,
    ).scalar_one()
    q(s, "INSERT INTO anticipa.case_status_history (case_id, to_status) VALUES (:c, 'identified')", c=case)
    # consent (A0 and a singleton), the simulated conversation of A0's case, and the snapshot rows
    for label in ("A0", "S5"):
        q(s, "INSERT INTO anticipa.consent_records (resident_id, scope, status, source) VALUES (:r, 'outreach', 'permit', 'synthetic_seed')", r=src.ids[label])
    q(
        s,
        "INSERT INTO anticipa.conversation_messages (care_gap_case_id, resident_id, role, body, language, source) "
        "VALUES (:c, :r, 'resident', 'hello', 'en', 'resident_input')",
        c=case, r=src.ids["A0"],
    )
    for key in ("core", "impact"):
        q(s, "INSERT INTO anticipa.analytics_snapshots (key, payload) VALUES (:k, CAST('{}' AS jsonb))", k=key)
    return src


def add_document(s: Session, store: LocalDocumentStore, src: Source, label: str, body: str) -> None:
    doc_id = uuid.uuid4()
    key = f"{SRC_VERSION}/{src.ids[label]}/{doc_id}.txt"
    store.write_text(key, body)
    q(
        s,
        "INSERT INTO anticipa.documents (id, resident_id, document_type, storage_key, content_hash) VALUES (:i, :r, 'clinical_note', :k, :h)",
        i=doc_id, r=src.ids[label], k=key, h=content_hash(body),
    )
    src.docs[label] = (doc_id, key)


@pytest.fixture
def world(db_session: Session, ddl_session: Session, tmp_path):
    source_store = LocalDocumentStore(tmp_path / "source")
    target_store = LocalDocumentStore(tmp_path / "target")
    src = build_source(db_session, source_store)
    return db_session, ddl_session, src, source_store, target_store


def run(world, **overrides):
    s, d, _src, source_store, target_store = world
    opts = {"source_version": SRC_VERSION, "target": 5, "max_residents": 8, "workers": 2}
    return js.run(raw(s), raw(d), source_store=source_store, target_store=target_store, **{**opts, **overrides})


def selected_labels(world) -> set[str]:
    _s, d, src, *_ = world
    ids = {r[0] for r in q(d, f"SELECT r.id FROM anticipa.residents r JOIN anticipa.datasets ds ON ds.id = r.dataset_id WHERE ds.dataset_version = '{JUDGE}'")}
    return {label for label, rid in src.ids.items() if rid in ids}


def family_of(label: str) -> set[str]:
    if label[0] in FAMILIES:
        return {f"{label[0]}{i}" for i in range(FAMILIES[label[0]])}
    return {label}


# ------------------------------------------------------------------------------------------------ tests


def test_dry_run_selects_whole_cohort_blocks_and_writes_nothing(world):
    s, d, src, *_ = world
    report = run(world, dry_run=True)
    assert report["dry_run"] is True and 5 <= report["residents"] <= 8
    assert n(d, "datasets", f"dataset_version = '{JUDGE}'") == 0
    # selection property: deterministic, whole families, cohort blocks only (cohort blocks hold 10 residents >= target)
    first, _ = js.select_residents(raw(s), SRC_VERSION, seed=42, target=5, max_residents=8)
    again, _ = js.select_residents(raw(s), SRC_VERSION, seed=42, target=5, max_residents=8)
    assert first == again
    labels = {label for label, rid in src.ids.items() if rid in set(first)}
    assert all(family_of(label) <= labels for label in labels)
    assert all(family_of(label) & COHORT for label in labels)
    other, _ = js.select_residents(raw(s), SRC_VERSION, seed=43, target=5, max_residents=8)
    assert len(other) >= 5


def test_selection_falls_back_to_residents_with_documents_when_the_cohort_is_too_small(world):
    s, _d, src, *_ = world
    ids, info = js.select_residents(raw(s), SRC_VERSION, seed=1, target=12, max_residents=14)
    labels = {label for label, rid in src.ids.items() if rid in set(ids)}
    assert COHORT <= {label for label in labels} or len(labels) >= 12
    assert info["blocks_by_stage"].get("residents_with_documents", 0) >= 1
    assert all(family_of(label) <= labels for label in labels)


def test_copy_is_complete_consistent_and_replaced_on_rerun(world):
    s, d, src, source_store, target_store = world
    report = run(world)
    chosen = selected_labels(world)
    assert 5 <= len(chosen) <= 8 and all(family_of(label) <= chosen for label in chosen)
    ids = [src.ids[label] for label in chosen]
    scoped = "resident_id = ANY(:r)"
    for table in ("clinical_records", "family_edges", "documents", "evidence_facts", "eligibility_evaluations", "care_gap_cases"):
        assert n(d, table, scoped, r=ids) == n(s, table, scoped, r=ids), table
    assert n(d, "residents") == len(chosen) and report["rows_copied"]["residents"] == len(chosen)
    assert n(d, "rule_versions") == n(s, "rule_versions") >= 1 and n(d, "guideline_versions", "review_status = 'approved'") >= 1
    assert n(d, "evidence_sources") == n(s, "evidence_sources", "evidence_fact_id IN (SELECT id FROM anticipa.evidence_facts WHERE resident_id = ANY(:r))", r=ids)
    assert n(d, "model_invocations") == len(chosen & COHORT) and n(d, "case_status_history") == (1 if "A0" in chosen else 0)
    # consent and conversation follow the chosen residents; `core` is recomputed on the target, every other snapshot row is copied
    assert n(d, "consent_records") == n(s, "consent_records", scoped, r=ids) == len(chosen & {"A0", "S5"})
    assert n(d, "conversation_messages") == (1 if "A0" in chosen else 0)
    assert [r[0] for r in q(d, "SELECT key FROM anticipa.analytics_snapshots ORDER BY key")] == ["impact"]
    # dataset row, key rewrite, text identity, cross-dataset pointers nulled
    assert q(d, "SELECT resident_target, manifest->'judge_subset'->>'source_dataset_version' FROM anticipa.datasets WHERE dataset_version = :v", v=JUDGE).one() == (len(chosen), SRC_VERSION)
    keys = [r[0] for r in q(d, "SELECT storage_key FROM anticipa.documents")]
    assert keys and all(k.startswith(JUDGE + "/") for k in keys)
    for key in keys:
        assert target_store.read_text(key) == source_store.read_text(SRC_VERSION + key[len(JUDGE) :])
    assert n(d, "evidence_facts", "subject_person_id IS NOT NULL") == (1 if {"A0", "A1"} <= chosen else 0)  # S0 -> S5 (outside) is nulled
    # every guard trigger is enabled again
    enabled = {r[0] for r in q(d, "SELECT tgname FROM pg_trigger WHERE tgname IN ('guideline_versions_guard', 'rule_versions_guard') AND tgenabled = 'O'")}
    assert enabled == {"guideline_versions_guard", "rule_versions_guard"}

    # source changes, then re-run: the target follows (also replacing the approved rule), stale text is removed
    victim = next(label for label in sorted(chosen) if label in WITH_DOCS and label not in COHORT)
    doc_id, old_key = src.docs[victim]
    q(s, "DELETE FROM anticipa.documents WHERE id = :i", i=doc_id)
    q(s, "INSERT INTO anticipa.clinical_records (resident_id, resource_type, source_resource_id, category, source_reference) VALUES (:r, 'Observation', 'new', 'observation', 'Observation/new')", r=src.ids[victim])
    second = run(world)
    assert n(d, "clinical_records", scoped, r=ids) == n(s, "clinical_records", scoped, r=ids)
    assert n(d, "documents", scoped, r=ids) == n(s, "documents", scoped, r=ids)
    stale_key = JUDGE + old_key[len(SRC_VERSION) :]
    assert not target_store.exists(stale_key) and second["stale_documents_removed"] >= 1
    assert n(d, "residents") == len(chosen) and n(d, "datasets", f"dataset_version = '{JUDGE}'") == 1
    assert n(d, "rule_versions", "approval_status = 'approved'") == n(s, "rule_versions", "approval_status = 'approved'")


def test_truncate_first_gives_the_same_result(world):
    _s, d, _src, _ss, target_store = world
    run(world)
    first = {t: n(d, t) for t in ("residents", "clinical_records", "documents", "rule_versions", "care_gap_cases")}
    report = run(world, truncate_first=True)
    assert {t: n(d, t) for t in first} == first and report["peak_mb_estimate"] >= 0
    assert n(d, "datasets", f"dataset_version = '{JUDGE}'") == 1


def test_refuses_when_the_target_holds_another_dataset(world):
    _s, d, *_ = world
    other = q(d, "INSERT INTO anticipa.datasets (dataset_version, generator) VALUES ('someone-else', 't') RETURNING id").scalar_one()
    q(d, "INSERT INTO anticipa.residents (dataset_id, source_system, source_patient_id) VALUES (:d, 't', 'x')", d=other)
    with pytest.raises(js.JudgeSubsetError, match="other dataset"):
        run(world)
    assert n(d, "residents") == 1 and n(d, "datasets", f"dataset_version = '{JUDGE}'") == 0  # untouched


def test_refuses_over_budget_missing_text_and_bad_versions(world):
    s, d, src, source_store, _t = world
    with pytest.raises(js.JudgeSubsetError, match="budget"):
        run(world, max_mb=0.000001)
    with pytest.raises(js.JudgeSubsetError, match="must differ"):
        run(world, judge_version=SRC_VERSION)
    with pytest.raises(js.JudgeSubsetError, match="not found"):
        run(world, source_version="does-not-exist")
    for label in COHORT & WITH_DOCS:  # lose the text of every cohort document: the selected set will include some
        source_store._path(src.docs[label][1]).unlink()
    with pytest.raises(js.JudgeSubsetError, match="missing"):
        run(world)
    assert n(d, "datasets", f"dataset_version = '{JUDGE}'") == 0  # nothing was loaded


def test_same_database_guard_and_conninfo():
    a = make_url("postgresql+psycopg://u:p@127.0.0.1:5433/anticipa")
    assert js._same_database(a, make_url("postgresql://x:y@127.0.0.1:5433/anticipa"))
    assert not js._same_database(a, make_url("postgresql://x:y@db.example.com:5432/postgres"))
    assert js._conninfo(a).startswith("postgresql://u:p@127.0.0.1:5433/anticipa")
