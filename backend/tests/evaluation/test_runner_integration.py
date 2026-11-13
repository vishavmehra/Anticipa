"""End-to-end evaluation run on the test database `anticipa_test` (rolled back) with a tiny synthetic ground-truth directory:
digest reproducibility (T-04), report/JSON consistency (T-09), persistence, API visibility and permissions, the test-split
guard, document/label sync and ground-truth tamper detection (FI-18)."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date

import builders as b
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from anticipa.datafabric.models import Dataset, Document, Resident
from anticipa.evaluation import gt, runner
from anticipa.evaluation.models import EvaluationRun
from anticipa.evidence.models import EvidenceFact, EvidenceSource

pytestmark = pytest.mark.integration
DV = "syn-20261108-s42"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def world(db_session, tmp_path):
    """Two residents (en, hi), one document each, facts for the English one; matching planted ground truth on disk."""
    ds = Dataset(dataset_version=f"syn-test-{uuid.uuid4().hex[:8]}", generator="test")
    db_session.add(ds)
    db_session.flush()
    out: dict = {"res": [], "docs": [], "labels": []}
    for i, (lang, text) in enumerate((("en", "Mother had colon cancer. Diabetes noted."), ("hi", "भाई को कैंसर था।")), start=1):
        r = Resident(dataset_id=ds.id, source_system="test", source_patient_id=uuid.uuid4().hex, birth_date=date(1974, 1, 1), administrative_sex="female",
                     preferred_language=lang, district_code=f"D{i}")  # fmt: skip
        db_session.add(r)
        db_session.flush()
        d = Document(resident_id=r.id, document_type="clinical_note", language=lang, storage_key=f"k/{r.id}", content_hash=sha(text), generation_method="template_v1",
                     processing_status="extracted")  # fmt: skip
        db_session.add(d)
        db_session.flush()
        out["res"].append(r)
        out["docs"].append((d, text))
    (d1, _), (d2, _) = out["docs"]
    r1, r2 = out["res"]
    l1 = b.label(1, res=str(r1.id), doc=str(d1.id), span=(0, 24), lang="en")  # relative, positive, historical
    l2 = b.label(2, res=str(r1.id), doc=str(d1.id), span=(26, 40), lang="en", subject="resident", relationship=None, category="condition", fact_type="diabetes", temporal="current")
    l3 = b.label(3, res=str(r2.id), doc=str(d2.id), span=(0, 14), lang="hi")
    out["labels"] = [l1, l2, l3]
    sha1, sha2 = sha(out["docs"][0][1]), sha(out["docs"][1][1])
    facts = [b.gt_fact(l1, str(d1.id), sha1), b.gt_fact(l2, str(d1.id), sha1), b.gt_fact(l3, str(d2.id), sha2)]
    index = [{"document_id": str(d1.id), "resident_id": str(r1.id), "split": "dev", "content_sha256": sha1, "language": "en"},
             {"document_id": str(d2.id), "resident_id": str(r2.id), "split": "dev", "content_sha256": sha2, "language": "hi"}]  # fmt: skip
    out["root"] = b.write_gt(tmp_path / "gt", DV, facts, index)
    out["gt_root"] = tmp_path / "gt"
    out["dataset"] = ds
    for lab, over in ((l1, {}), (l2, {"assertion_status": "uncertain"})):  # l2 comes back with the wrong assertion
        fact = EvidenceFact(resident_id=r1.id, subject_type=lab.subject_type, category=lab.category, fact_type=lab.fact_type, value={"concept": lab.fact_type},
                            assertion_status=over.get("assertion_status", lab.assertion), temporal_status=lab.temporal, relationship_type=lab.relationship,
                            validation_status="accepted", extraction_method="ai_assisted", model_provider="claude", model_id="haiku-test",
                            prompt_version="evidence.extract@1", extraction_confidence="high")  # fmt: skip
        db_session.add(fact)
        db_session.flush()
        db_session.add(EvidenceSource(evidence_fact_id=fact.id, document_id=d1.id, source_quote="q", source_start_offset=lab.spans[0][0],
                                      source_end_offset=lab.spans[0][1], verification_status="verified_exact"))  # fmt: skip
    db_session.flush()
    out["sessions"] = sessionmaker(bind=db_session.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False)
    out["ids"] = [str(r1.id), str(r2.id)]
    out["tmp"] = tmp_path
    return out


def cfg(w, **kw):
    base = {"dataset_version": DV, "resident_ids": w["ids"], "gt_root": str(w["gt_root"]), "out_dir": str(w["tmp"] / "out"), "docs_dir": str(w["tmp"] / "docs"),
            "probe_closure": 0, "include_regression": False, "bootstrap_resamples": 50}  # fmt: skip
    base.update(kw)
    return runner.RunConfig(**base)


def numbers(obj, acc=None):
    acc = set() if acc is None else acc
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, (int, float)):
        acc.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            numbers(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            numbers(v, acc)
    return acc


def test_run_scores_by_language_persists_and_is_reproducible(world):
    w = world
    rep = runner.run_evaluation(w["sessions"], cfg(w))
    M = rep["metrics"]
    en, hi = (M["EX-02.A"]["breakdowns"]["language"][lg] for lg in ("en", "hi"))
    assert (en["numerator"], en["denominator"]) == (1, 2) and (hi["numerator"], hi["denominator"]) == (0, 1)  # l2 wrong assertion; l3 never extracted
    assert M["EX-09"]["numerator"] == 1 and M["EX-09"]["denominator"] == 2 and M["EX-06"]["value"] == 1.0
    assert rep["status"] == "complete" and rep["run_type"] == "dev" and rep["ground_truth"]["checksum_verified"] is True
    assert rep["coverage"][1]["tier"] == "ai_evidence_cohort" and rep["coverage"][1]["actual_completed"] == 2 and rep["coverage"][1]["target"] == 10_000
    assert rep["scope"]["extraction"]["documents_evaluated"] == 2 and rep["synthetic_banner"].startswith("Results are measured against planted ground truth")
    assert {g["threshold_status"] for g in rep["gates"] if g["tier"] == 3} == {"proposal"}
    # persisted: aggregate only, no labels
    with w["sessions"]() as s:
        row = s.execute(select(EvaluationRun).where(EvaluationRun.run_id == rep["run_id"])).scalar_one()
        assert row.metrics_digest == rep["metrics_digest"] and row.dataset_version == DV and row.split == "dev" and row.gate_summary == rep["gate_summary"]
        blob = json.dumps(row.details) + json.dumps(row.metrics)
        assert not any(k in blob for k in ("document_content_sha256", "expected_present", "acceptable_quote_spans")) and row.synthetic_status == "synthetic"
    # T-04: the same stored results give the identical digest
    again = runner.run_evaluation(w["sessions"], cfg(w, persist=False, write_files=False))
    assert again["metrics_digest"] == rep["metrics_digest"] and again["run_id"] != rep["run_id"]


def test_report_files_are_atomic_and_markdown_numbers_come_from_the_json(world):
    w = world
    rep = runner.run_evaluation(w["sessions"], cfg(w))
    out = w["tmp"] / "out"
    js, md = json.loads((out / "report.json").read_text(encoding="utf-8")), (out / "report.md").read_text(encoding="utf-8")
    assert js["run_id"] == rep["run_id"] and not list(out.glob("*.tmp"))
    assert (w["tmp"] / "docs" / f"evaluation_report_{rep['created_at'][:10]}_dev.md").read_text(encoding="utf-8") == md
    known = numbers(js)
    md_numbers = re.sub(r"§\d+(\.\d+)*", "", md.split("## 15.")[0])  # spec section references (§15.3) are not measurements; section 15 is static policy prose (thresholds of the matching rules), not a measurement
    ctx_dump = {m.group(0): md_numbers[max(0, m.start() - 60): m.end() + 20] for m in re.finditer(r"(?<![\w.-])\d+\.\d+(?![\w.-])", md_numbers)}
    decimals = {m for m in re.findall(r"(?<![\w.-])\d+\.\d+(?![\w.-])", re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.+Z-]+|\d{8}T\d+Z-\w+", "", md_numbers))}
    stray = [d for d in decimals if not any(abs(float(d) - k) <= 5e-5 for k in known)]
    assert stray == [], {d: ctx_dump.get(d) for d in stray}
    for must in ("## 2. Coverage: target vs actual", "## 14. Limitations", "proposals pending Q-037"):
        assert must.lower() in md.lower()
    assert "not a clinical validation" in md.lower() and md.index("## 3.") < md.index("## 4.") < md.index("## 14.")


def test_test_split_needs_final_and_is_logged(world):
    w = world
    with pytest.raises(runner.FinalRequired):
        runner.run_evaluation(w["sessions"], cfg(w, split="test"))
    assert gt.prior_final_runs(w["gt_root"], DV) == 0
    rep = runner.run_evaluation(w["sessions"], cfg(w, split="all", final=True, persist=False, write_files=False))
    assert rep["run_type"] == "test_final" and gt.prior_final_runs(w["gt_root"], DV) == 1  # the access ledger recorded it


def test_label_document_hash_mismatch_aborts_and_altered_ground_truth_is_refused(world, db_session):
    w = world
    (d1, _), _ = w["docs"]
    db_session.execute(Document.__table__.update().where(Document.id == d1.id).values(content_hash="0" * 64))
    db_session.flush()
    with pytest.raises(gt.GtDocumentMismatch):
        runner.run_evaluation(w["sessions"], cfg(w, persist=False, write_files=False))
    db_session.execute(Document.__table__.update().where(Document.id == d1.id).values(content_hash=sha(w["docs"][0][1])))
    p = w["root"] / "facts" / "planted_facts.dev.jsonl"
    p.write_bytes(p.read_bytes() + b"\n")
    with pytest.raises(gt.GtIntegrityError):
        runner.run_evaluation(w["sessions"], cfg(w, persist=False, write_files=False))


def test_unprocessed_documents_are_not_scored_as_misses(world, db_session):
    w = world
    (_, _), (d2, _) = w["docs"]
    db_session.execute(Document.__table__.update().where(Document.id == d2.id).values(processing_status="pending"))
    db_session.flush()
    rep = runner.run_evaluation(w["sessions"], cfg(w, persist=False, write_files=False))
    assert rep["metrics"]["EX-02.A"]["denominator"] == 2 and rep["scope"]["extraction"]["labels_in_unprocessed_documents"] == 1
    assert rep["scope"]["unprocessed_documents"] == 1


def test_api_exposes_the_latest_run_to_observers_only_aggregates(world, api_client, auth_headers):
    w = world
    rep = runner.run_evaluation(w["sessions"], cfg(w, write_files=False))
    r = api_client.get("/api/v1/analytics/evaluation", headers=auth_headers("observer"))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["run_id"] == rep["run_id"] and data["metrics"]["EX-02.A"]["breakdowns"]["language"]["en"]["denominator"] == 2 and data["gate_summary"]
    body = r.text
    assert '"quote"' not in body and "planted_facts" not in body and "expected_outcome" not in body
    runs = api_client.get("/api/v1/analytics/evaluation/runs", headers=auth_headers("observer"))
    assert runs.status_code == 200 and runs.json()["meta"]["total"] >= 1 and "metrics" not in runs.json()["data"][0]
    one = api_client.get(f"/api/v1/analytics/evaluation/runs/{rep['run_id']}", headers=auth_headers("observer"))
    assert one.status_code == 200 and one.json()["data"]["metrics_digest"] == rep["metrics_digest"]
    assert api_client.get("/api/v1/analytics/evaluation/runs/nope", headers=auth_headers("observer")).status_code == 404


def test_api_permissions(world, api_client, auth_headers):
    assert api_client.get("/api/v1/analytics/evaluation").status_code == 401
    assert api_client.get("/api/v1/analytics/evaluation", headers=auth_headers("care_coordinator")).status_code == 403  # analytics.view_limited only
    body = {"dataset_version": DV}
    assert api_client.post("/api/v1/analytics/evaluation/runs", json=body, headers=auth_headers("observer")).status_code == 403  # read-only role
    ok = api_client.post("/api/v1/analytics/evaluation/runs", json=body, headers=auth_headers("system_admin"))
    assert ok.status_code == 202 and ok.json()["data"]["created"] is True
    assert api_client.post("/api/v1/analytics/evaluation/runs", json={"dataset_version": "bad"}, headers=auth_headers("system_admin")).status_code == 422
    again = api_client.post("/api/v1/analytics/evaluation/runs", json=body, headers=auth_headers("system_admin"))
    assert again.json()["data"]["job_id"] == ok.json()["data"]["job_id"] and again.json()["data"]["created"] is False  # idempotent


def test_job_handler_is_registered():
    from anticipa.evaluation import handlers  # noqa: F401
    from anticipa.jobs.registry import get_handler

    assert get_handler("evaluation.run") is not None


BANNED = re.compile(r"synthetic|illustrative|pending|—|–|\w-\w|\s-\s", re.IGNORECASE)


def test_text_the_ui_renders_follows_the_owner_copy_rule(world):
    """Owner rule: no dashes and no synthetic/illustrative/pending wording in rendered text (field NAMES are exempt)."""
    from anticipa.evaluation import report

    rep = runner.run_evaluation(world["sessions"], cfg(world, persist=False, write_files=False))
    rendered = [rep["synthetic_banner"], report.BANNER, *report.LIMITATIONS, *rep["limitations"], rep["environment"]["description"]]
    rendered += [f"{e['item']} {e['reason']}" for e in rep["exclusions"]] + [g["threshold_status"] for g in rep["gates"]] + [c["note"] + " " + c["tier"].replace("_", " ") for c in rep["coverage"]] + [rep["run_type"].replace("_", " "), rep["status"]]
    offenders = [t for t in rendered if BANNED.search(t)]
    assert offenders == [], offenders
    assert isinstance(rep["safety_cases"], list) and isinstance(rep["safety_case_summary"], dict)  # the array the Command Center lists


def test_dev_only_error_analysis_counts_error_types_and_gives_examples_with_quotes(world, tmp_path):
    from anticipa.evaluation import error_analysis

    w = world
    texts = {str(w["docs"][0][0].id): w["docs"][0][1], str(w["docs"][1][0].id): w["docs"][1][1]}
    rep = error_analysis.analyse(w["sessions"], DV, w["ids"], gt_root=w["gt_root"], text_of=texts.get)
    assert rep["split"] == "dev" and rep["totals"]["labels_scored"] == 3 and rep["totals"]["strict_correct"] == 1 and rep["totals"]["strict_misses"] == 2
    assert rep["strict_misses_by_type"] == {"attribute_mismatch": 1, "not_detected": 1}
    assert rep["assertion_confusion_gold_to_predicted"] == {"positive>uncertain": 1} and rep["attribute_mismatch_combinations"] == {"assertion": 1}
    assert rep["totals"]["accepted_false_positives"] == 1  # the right concept and span, wrong assertion, seen from the prediction side
    assert 1 <= len(rep["examples"]) <= 40 and all(e["kind"] in ("strict_miss", "false_positive") for e in rep["examples"])
    first = next(e for e in rep["examples"] if e["error_type"] == "not_detected")
    assert first["label"]["quote"] == w["docs"][1][1][0:14] and "context" in first and first["prediction"] is None
    assert not (w["gt_root"] / DV / "test_access.jsonl").exists()  # the ledger is never touched: the test split is not read
    out = error_analysis.write_json(rep, tmp_path / "x" / "errors.json")
    assert json.loads(out.read_text(encoding="utf-8"))["schema_version"] == "anticipa.eval.extraction_errors.v1" and not list(out.parent.glob("*.tmp"))


def test_dry_check_reads_no_label_and_touches_no_ledger(world, monkeypatch):
    w = world
    opened: list[str] = []
    real = gt.iter_jsonl

    def spy(path):
        opened.append(str(path).replace("\\", "/"))
        return real(path)

    monkeypatch.setattr(gt, "iter_jsonl", spy)
    out = runner.dry_check(w["sessions"], cfg(w, split="test", final=True))
    assert not any("/facts/" in p or p.endswith("test_access.jsonl") for p in opened), opened  # no label file was opened
    assert not (w["gt_root"] / DV / "test_access.jsonl").exists() and out["prior_final_runs"] == 0
    assert out["ground_truth"]["checksums_verified"] is True and out["document_hash_mismatches"] == 0 and out["documents_in_selected_split"] == 0  # the world has dev docs only
    assert out["ready"] is False and any("planning" in b for b in out["blockers"])  # downstream rows are missing in the test database
    dev = runner.dry_check(w["sessions"], cfg(w, split="dev"))
    assert dev["documents_in_selected_split"] == 2 and dev["documents_by_status"] == {"extracted": 2}
    (w["root"] / "facts" / "planted_facts.dev.jsonl").write_bytes(b"{}\n")
    with pytest.raises(gt.GtIntegrityError):  # a tampered ground truth file fails the preflight too
        runner.dry_check(w["sessions"], cfg(w, split="dev"))


def test_api_serves_the_final_run_first_with_the_field_names_the_evaluation_page_reads(world, api_client, auth_headers):
    w = world
    with w["sessions"]() as s:  # a final run that is OLDER than the dev run below must still be the one served
        s.add(EvaluationRun(run_id="20200101T000000000000Z-final000", status="complete", run_type="test_final", dataset_version=DV, split="test",
                            cohort_id="c1", ground_truth_id="gt-x", metrics_digest="d" * 64, gate_summary={"pass": 1}, metrics={"EX-01.A": {"value": 0.5}},
                            gates=[{"gate_id": "SG-02", "metric_id": "EX-01.A", "comparator": ">=", "threshold": 0.95, "threshold_status": "proposal", "observed": 0.5, "result": "fail"}],
                            details={"coverage": [{"tier": "intensive_evaluation", "target": 10000, "actual_completed": 1}], "safety_cases": [], "limitations": ["x"],
                                     "exclusions": [], "code": {"git_commit": "abc", "dirty": False}, "ground_truth": {"ground_truth_id": "gt-x", "frozen": False}}))  # fmt: skip
        s.commit()
    runner.run_evaluation(w["sessions"], cfg(w, write_files=False))  # a newer dev run
    data = api_client.get("/api/v1/analytics/evaluation", headers=auth_headers("observer")).json()["data"]
    assert data["run_id"] == "20200101T000000000000Z-final000" and data["run_type"] == "test_final"
    for field in ("run_id", "created_at", "run_type", "status", "synthetic_banner", "code", "dataset", "ground_truth", "coverage", "metrics", "gates", "safety_cases",
                  "exclusions", "limitations"):  # the keys of the page's EvaluationReport type
        assert field in data, field
    assert data["dataset"]["dataset_version"] == DV and data["dataset"]["split"] == "test" and data["gates"][0]["gate_id"] == "SG-02"
