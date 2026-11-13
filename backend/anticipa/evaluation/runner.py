"""Evaluation run orchestration (Docs/09 §9.2, §10): read stored pipeline results + planted ground truth, compute every
metric deterministically (NO model calls), evaluate gates, persist one aggregate `evaluation_runs` row, write the report.

Callers: cli.py (`python -m anticipa.evaluation run`), handlers.py (job `evaluation.run`), routes.py. The evaluator
only reads pipeline tables and ground truth; its single write is its own `evaluation_runs` row (+ an audit event).
Instruction: deliver a working, tested, demonstrable Anticipa MVP including the evaluation report.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from anticipa.config import ROOT_DIR
from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.evaluation import (
    closure_eval,
    eligibility,
    extraction,
    gates,
    perf,
    planning_eval,
    readers,
    report,
)
from anticipa.evaluation import gt as gtmod
from anticipa.evaluation.matching import Label
from anticipa.evaluation.models import EvaluationRun
from anticipa.evaluation.stats import LOW_N, r6

SessionFactory = Callable[[], Session]
EVALUATED = readers.EVALUATED_STATUSES
REPORTS_DIR = ROOT_DIR / "data" / "reports"
DOCS_DIR = ROOT_DIR / "Docs" / "evaluation"
MATCHING_POLICY = (
    "Facts and labels are matched per document, one-to-one, greedily by verified-span overlap (overlap coefficient >= 0.5, original code points); model-reported offsets are never used.",
    "L1 detection = overlap + compatible category (equal or the label's acceptable_categories). fact_type equality is not required because fact_type is free text chosen by the model.",
    "L3 lenient = L1 + concept match (token Jaccard >= 0.6 or full multi token containment between predicted fact_type/concept and the label's fact_type/concept/English value text, small synonym table; medications also match on a shared ingredient word, a rule added after inspecting dev results only).",
    "L2 strict = L3 + subject_type (+ relationship for relatives) + assertion + temporal all equal. Duplicate predictions are reported, not counted as false positives.",
    "Views: C = every persisted fact, A = accepted, T = accepted + routed to review. Only condition, family_history, medication, observation, preventive_care and procedure facts are scored.",
    "Extraction metrics cover documents with processing_status extracted or extraction_failed; labels of unprocessed documents are counted separately and never as misses. Failed documents contribute false negatives.",
)


class FinalRequired(gtmod.GtError):
    code = "FINAL_REQUIRED"


@dataclass
class RunConfig:
    dataset_version: str
    split: str = "dev"
    cohort_manifest: str | None = None
    resident_ids: list[str] | None = None
    cohort_id: str | None = None
    final: bool = False
    gt_root: str | None = None
    verify_checksums: bool = True
    bootstrap_resamples: int = 200
    seed: int = 42
    probe_closure: int = 20
    profile: str = "dev"
    out_dir: str | None = None
    docs_dir: str | None = None
    write_files: bool = True
    persist: bool = True
    include_regression: bool = True
    environment: str = "local development database with generated data"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def run_type(self) -> str:
        return "dev" if self.split == "dev" else "test_final"

    def hashable(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("out_dir", "docs_dir", "write_files", "persist", "environment", "profile"):
            d.pop(k, None)
        return d


def _unavailable(reason: str, metric: str) -> dict[str, Any]:
    return {"value": None, "status": "not_evaluated", "metric": metric, "reason": reason}


def _language_gap(M: dict[str, Any]) -> dict[str, Any]:
    cells = (M["EX-03.A"].get("breakdowns") or {}).get("language", {})
    ok = {k: v["value"] for k, v in cells.items() if v.get("value") is not None and v.get("n", 0) >= LOW_N}
    if len(ok) < 2:
        return {**_unavailable("fewer than two languages with a measured F1 and n >= 30", "language gap in strict F1 (A)"), "by_language": cells}
    return {"value": r6((max(ok.values()) - min(ok.values())) * 100), "unit": "percentage points", "status": "measured", "n": len(ok),
            "by_language": cells, "metric": "max - min strict F1 (view A) across languages with n >= 30"}  # fmt: skip


def run_evaluation(sessions: SessionFactory, cfg: RunConfig, *, actor_id: uuid.UUID | None = None, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    say = progress or (lambda _m: None)
    t0 = time.perf_counter()
    started = datetime.now(UTC)
    if cfg.split in ("test", "all") and not cfg.final:
        raise FinalRequired("evaluating the test split is a deliberate, logged act: pass final=True / --final (09 §5.6)")
    root = Path(cfg.gt_root) if cfg.gt_root else None
    # ---- cohort and ground truth
    cohort_id, cohort_sha, ids = cfg.cohort_id, None, cfg.resident_ids
    if ids is None and cfg.cohort_manifest:
        m = readers.load_cohort_manifest(cfg.cohort_manifest)
        ids, cohort_id, cohort_sha = m["resident_ids"], m.get("cohort_id"), m.get("resident_ids_sha256")
    if ids is None:  # no cohort named: evaluate every resident whose documents have been through extraction
        with sessions() as s0:
            ids = readers.processed_resident_ids(s0, cfg.dataset_version)
            s0.rollback()
        cohort_id = cohort_id or "all residents with processed documents"
    say(f"loading ground truth for {len(ids)} residents")
    gt = gtmod.load_ground_truth(cfg.dataset_version, resident_ids=ids, split=cfg.split, root=root, verify=cfg.verify_checksums)
    prior_finals = gtmod.prior_final_runs(root, cfg.dataset_version)
    config_hash = report.digest({**cfg.hashable(), "ground_truth_id": gt.ground_truth_id})
    run_id = f"{started.strftime('%Y%m%dT%H%M%S%f')}Z-{config_hash[:8]}"
    if cfg.split in ("test", "all"):
        gtmod.append_test_access(gt, actor=gtmod.actor_name(), run_id=run_id, command=f"evaluation run --split {cfg.split} --final")
    labelled_residents = {d["resident_id"] for d in gt.doc_index.values()}
    labels = [Label.from_gt(f) for f in gt.facts]

    # ---- pipeline results (read-only)
    say("reading pipeline results")
    exclusions: list[dict[str, Any]] = []
    with sessions() as s:
        db_docs = readers.load_documents(s, sorted(labelled_residents))
        docs: dict[str, extraction.DocInfo] = {}
        mismatched = missing = unprocessed = 0
        for doc_id, rec in gt.doc_index.items():
            hit = db_docs.get(doc_id)
            if hit is None:
                missing += 1
                continue
            info, content_hash = hit
            if content_hash != rec["content_sha256"]:
                mismatched += 1
            if info.status in EVALUATED:
                docs[doc_id] = info
            else:
                unprocessed += 1
        if mismatched:
            raise gtmod.GtDocumentMismatch(f"{mismatched} document(s) differ from the hash recorded in the ground truth; documents and labels are out of sync")
        preds = readers.load_predictions(s, list(docs))
        eval_residents = sorted({d.resident_id for d in docs.values()})
        say(f"scoring extraction ({len(docs)} documents, {len(preds)} persisted facts)")
        ex = extraction.evaluate_extraction(labels, preds, docs, conflict_groups=gt.conflicts, seed=cfg.seed, bootstrap_resamples=cfg.bootstrap_resamples)
        matched = ex.pop("_matched_label_ids")
        M: dict[str, Any] = dict(ex["metrics"])
        residents = readers.load_residents(s, eval_residents)
        evals = readers.latest_evaluations(s, eval_residents)
        say("scoring eligibility, planning, closure, cost")
        mode_e = eligibility.evaluate_mode_e(evals, residents, gt.enrichment, as_of=gt.as_of, matched_label_ids=matched)
        plan_runs: list[dict[str, Any]] = []
        for run in readers.completed_planning_runs(s, limit=5):
            rows = readers.planning_rows(s, run["id"])
            cap = readers.capacity_by_district(s, run["planning_period_start"], run["planning_period_end"])
            cases = readers.cases_for_planning(s, [r["care_gap_case_id"] for r in rows["allocations"]])
            plan_runs.append(planning_eval.evaluate_run(run, rows, cap, existing_cases=cases, allocations_total=rows["allocations_total"],
                                                        reason_codes=_reason_codes()))  # fmt: skip
        closure_data = readers.closure_rows(s, eval_residents)
        closure = closure_eval.evaluate_closure(closure_data, residents)
        invocations = readers.load_invocations(s)
        jobs = readers.load_extraction_jobs(s)
        structured = readers.structured_counts(s, cfg.dataset_version)
        n_processed_residents = len(readers.processed_resident_ids(s, cfg.dataset_version))
        s.rollback()
    probe = closure_eval.probe_invalid_closures(sessions, sample=cfg.probe_closure) if cfg.probe_closure else {"attempts": 0, "rejected": 0, "accepted_in_error": 0, "error_codes": {}}
    closure["LC-02"] = closure_eval.probe_metric(probe)
    regression = eligibility.run_regression() if cfg.include_regression else None

    # ---- assemble metrics
    failed_docs = sum(1 for d in docs.values() if d.status != "extracted")
    for k, v in mode_e.items():
        if k.startswith("EL-"):
            M[k] = v
    if regression:
        M.update(eligibility.regression_metrics(regression))
    else:
        for k in ("EL-02", "EL-08"):
            M[k] = _unavailable("regression suite not run", k)
    planning = planning_eval.aggregate(plan_runs, planning_eval.declared_constraints())
    if plan_runs:
        planning["runs"] = plan_runs
        for k in ("CP-01", "CP-04", "CP-09"):
            M[k] = planning[k]
    else:
        for k in ("CP-01", "CP-04", "CP-09"):
            M[k] = _unavailable("no completed planning run exists", k)
    for k in ("LC-01", "LC-03", "LC-04", "LC-05", "EQ-08", "EQ-09"):
        M[k] = closure[k]
    M["LC-02"] = closure["LC-02"]
    performance = perf.evaluate_perf(
        invocations, jobs, linked_invocation_ids={p.invocation_id for p in preds if p.invocation_id}, documents_evaluated=len(docs),
        documents_failed=failed_docs, residents_in_cohort=len(ids), residents_processed=n_processed_residents,
    )  # fmt: skip
    M.update(performance)
    M["EQ-06"] = _language_gap(M)
    gaps = {}
    if plan_runs:
        for dim in ("district_code", "language"):
            e = plan_runs[0]["equity"][dim]
            gaps[dim] = {"parity_gap_points": e["parity_gap_points"], "parity_ratio": e["parity_ratio"], "groups_used": e["groups_used"]}
    gap_values = [g["parity_gap_points"] for g in gaps.values() if g["parity_gap_points"] is not None]
    M["EQ-02"] = {"value": max(gap_values), "unit": "percentage points", "status": "measured", "by_dimension": gaps, "metric": "largest allocation parity gap (latest planning run, SIMULATED capacity)"} if gap_values else _unavailable("no planning run with at least two groups of >= 30 cases", "allocation parity gap")  # fmt: skip
    M["RET-01"] = _unavailable("no retrieval ground truth exists and the semantic-vs-FTS benchmark needs an approved cost estimate (ADR-065)", "retrieval Recall@10")
    M["LBL-02"] = _unavailable("no human-reviewed label sample exists; every label is programmatic", "label-error estimate")
    for k, why in (("PF-07", "structured-processing scale is measured by the data-fabric benchmark logs, not re-measured here"),
                   ("PF-08", "guideline-evaluation scale is measured by the guideline benchmark, not re-measured here"),
                   ("PF-09", "resumability needs an injected worker kill; covered by the jobs module's own tests")):  # fmt: skip
        M[k] = _unavailable(why, k)

    partial = False
    gate_list = gates.evaluate_gates(M, partial=partial, checksum_verified=gt.checksum_verified)
    summary = gates.summarise(gate_list)
    models = sorted({(p.provider, p.model_id, p.prompt_version) for p in preds if p.provider} | {(i["provider"], i["model_id"], i["prompt_version"]) for i in invocations}, key=str)  # fmt: skip
    exclusions += [{"item": "ground truth documents missing from the database", "reason": str(missing)},
                   {"item": "documents not yet processed by extraction (their labels are not scored)", "reason": str(unprocessed)},
                   {"item": "labels in unprocessed documents", "reason": str(ex["scope"]["labels_in_unprocessed_documents"])},
                   {"item": "Mode E evaluations skipped", "reason": ", ".join(f"{k}: {v}" for k, v in mode_e["scope"]["skipped"].items()) or "none"}]  # fmt: skip
    errors = Counter(i["error_category"] for i in invocations if i["error_category"])
    equity_summary = {
        "EQ-06 extraction F1 gap between languages (points)": M["EQ-06"]["value"],
        "EQ-02 largest allocation parity gap (points)": M["EQ-02"]["value"],
        "report flag threshold (proposal, points)": 10,
        **{f"{d} parity gap (points)": g["parity_gap_points"] for d, g in gaps.items()},
        **{f"{d} parity ratio": g["parity_ratio"] for d, g in gaps.items()},
    }  # fmt: skip
    metrics_only = {k: v for k, v in M.items()}
    rep: dict[str, Any] = {
        "schema_version": report.SCHEMA_VERSION, "run_id": run_id, "created_at": started.isoformat(timespec="seconds"),
        "run_type": cfg.run_type, "status": "partial" if partial else "complete", "synthetic_banner": report.BANNER,
        "code": report.git_identity(),
        "dataset": {"dataset_version": cfg.dataset_version, "split": cfg.split, "cohort_id": cohort_id, "cohort_resident_ids_sha256": cohort_sha,
                    "cohort_residents": len(ids) if ids is not None else None},  # fmt: skip
        "ground_truth": {"ground_truth_id": gt.ground_truth_id, "gt_manifest_sha256": gt.manifest_sha256, "checksum_verified": gt.checksum_verified,
                         "frozen": False, "label_tiers": gt.tiers(), "prior_final_runs": prior_finals,
                         "labels_loaded": len(gt.facts), "note": "no gt_manifest/CHANGELOG/freeze layer exists; integrity is the generator's per-file sha256"},  # fmt: skip
        "environment": {"description": cfg.environment, "tiers": "extraction: AI evidence cohort; eligibility/planning/closure: same local database; cost: model_invocations"},
        "coverage": perf.coverage(structured=structured, ai_residents=len({d.resident_id for d in docs.values() if d.status == "extracted"}),
                                  evaluated_residents=len(eval_residents), labelled_residents=len(labelled_residents)),  # fmt: skip
        "configuration": {"models": [{"provider": a, "model_id": b, "prompt_version": c} for a, b, c in models],
                          "seeds": {"bootstrap": cfg.seed}, "bootstrap_resamples": cfg.bootstrap_resamples, "hardware": perf.hardware(),
                          "matching_policy": list(MATCHING_POLICY), "config_sha256": config_hash,
                          "evaluation_as_of_date": gt.as_of.isoformat(), "cache": {"mode": "stored pipeline results (no replay store used)"}},  # fmt: skip
        "scope": {"extraction": ex["scope"], "unprocessed_documents": unprocessed, "documents_missing_in_database": missing},
        "metrics": metrics_only, "gates": gate_list, "gate_summary": summary, "safety_cases": _flat_cases(ex["safety_cases"]),
        "safety_case_summary": ex["safety_cases"],
        "eligibility": {"mode_r": {**(regression or {"cases": 0, "passed": 0, "failures": []}), "environment": "in-memory replay of rules_draft.json test cases through the engine (no database, no model)"},
                        "mode_e": {k: v for k, v in mode_e.items() if not k.startswith("EL-")}},  # fmt: skip
        "planning": planning, "equity": {"summary": equity_summary, "allocation_by_group": ({d: plan_runs[0]["equity"][d]["allocation_rate_by_group"] for d in ("district_code", "language")} if plan_runs else {})},  # fmt: skip
        "closure": closure, "performance": performance, "exclusions": exclusions,
        "failures": {"documents_failed": failed_docs, "by_error_category": dict(sorted(errors.items()))},
        "replay": {"replayed_calls": 0, "live_calls_by_evaluator": 0, "note": "the evaluator makes no provider calls; figures describe the recorded pipeline run"},
        "projections": [], "limitations": list(report.LIMITATIONS) + ([] if gt.checksum_verified else [
            "Ground truth file checksums were not verified in this run because the generator manifest was out of date. Document hashes were still checked against the database, and the final run needs verified checksums."]),
        "timing": {"evaluation_seconds": round(time.perf_counter() - t0, 2)},
    }  # fmt: skip
    rep["metrics_digest"] = report.digest({"metrics": rep["metrics"], "gates": rep["gates"]})
    rep["exit_code"] = gates.exit_code(gate_list, partial=partial, profile=cfg.profile)
    if cfg.write_files:
        out = Path(cfg.out_dir) if cfg.out_dir else REPORTS_DIR / run_id
        rep["files"] = report.write_outputs(rep, out, Path(cfg.docs_dir) if cfg.docs_dir else None)
    if cfg.persist:
        persist(sessions, rep, cfg, actor_id=actor_id, started=started)
    return rep


def _flat_cases(summary: dict[str, Any], per_metric: int = 50) -> list[dict[str, str]]:
    """The spec's `safety_cases` array: [{metric_id, case_ref}] (ids only, capped per metric; counts live in the summary)."""
    return [
        {"metric_id": mid, "case_ref": "document {} fact {} label {}".format(*(str(c.get(k) or "none")[:8] for k in ("document_id", "fact_id", "label_id")))}
        for mid, v in summary.items() for c in v["cases"][:per_metric]
    ]  # fmt: skip


def dry_check(sessions: SessionFactory, cfg: RunConfig) -> dict[str, Any]:
    """Preflight of a run (typically the final one) WITHOUT scoring: ground truth checksums and document index, cohort, document
    hash sync with the database, extraction and downstream readiness. Reads no label and writes no ledger entry."""
    root = Path(cfg.gt_root) if cfg.gt_root else None
    ids = cfg.resident_ids
    cohort_id = cfg.cohort_id
    if ids is None and cfg.cohort_manifest:
        m = readers.load_cohort_manifest(cfg.cohort_manifest)
        ids, cohort_id = m["resident_ids"], m.get("cohort_id")
    blockers: list[str] = []
    pre = gtmod.preflight(cfg.dataset_version, resident_ids=ids, root=root, verify=cfg.verify_checksums)
    use = ("dev", "test") if cfg.split == "all" else (cfg.split,)
    index = {i: r for i, r in pre["doc_index"].items() if r["split"] in use}
    with sessions() as s:
        db_docs = readers.load_documents(s, sorted({r["resident_id"] for r in index.values()}))
        status: Counter = Counter()
        mismatched = missing = 0
        for doc_id, rec in index.items():
            hit = db_docs.get(doc_id)
            if hit is None:
                missing += 1
                continue
            mismatched += hit[1] != rec["content_sha256"]
            status[hit[0].status] += 1
        counts = readers.readiness_counts(s)
        s.rollback()
    if mismatched:
        blockers.append(f"{mismatched} document hashes differ from the ground truth")
    if missing:
        blockers.append(f"{missing} ground truth documents are missing from the database")
    unprocessed = sum(n for k, n in status.items() if k not in EVALUATED)
    if unprocessed:
        blockers.append(f"{unprocessed} documents in the selected split have not been through extraction")
    if counts["population_jobs_active"]:
        blockers.append("population evaluation is still running")
    for key, why in (("planning_runs_completed", "no completed planning run"), ("communication_events", "no outreach rows"), ("closure_rows", "no closure rows")):
        if counts[key] < 1:
            blockers.append(why)
    return {
        "split": cfg.split, "dataset_version": cfg.dataset_version, "cohort_id": cohort_id, "cohort_residents": len(ids) if ids is not None else None,
        "ground_truth": {k: v for k, v in pre.items() if k != "doc_index"}, "documents_in_selected_split": len(index),
        "documents_by_status": dict(status), "document_hash_mismatches": mismatched, "documents_missing_in_database": missing, "readiness": counts,
        "prior_final_runs": gtmod.prior_final_runs(root, cfg.dataset_version), "blockers": blockers, "ready": not blockers,
        "note": "preflight only: no label was read, no score was computed, the test access ledger was not touched",
    }  # fmt: skip


def _reason_codes() -> Sequence[str]:
    from anticipa.planning.domain import REASON_CODES

    return REASON_CODES


def persist(sessions: SessionFactory, rep: dict[str, Any], cfg: RunConfig, *, actor_id: uuid.UUID | None, started: datetime) -> None:
    """One aggregate row (no labels, no text) plus an audit event."""
    details = {k: rep[k] for k in ("coverage", "scope", "safety_cases", "safety_case_summary", "eligibility", "planning", "equity", "closure", "exclusions", "failures",
                                   "limitations", "environment", "ground_truth", "code", "replay", "projections", "timing") if k in rep}  # fmt: skip
    with sessions() as s:
        s.add(EvaluationRun(
            run_id=rep["run_id"], status=rep["status"], run_type=rep["run_type"], dataset_version=cfg.dataset_version, split=cfg.split,
            cohort_id=rep["dataset"].get("cohort_id"), ground_truth_id=rep["ground_truth"]["ground_truth_id"], metrics_digest=rep["metrics_digest"],
            config={**cfg.hashable(), "matching_policy": rep["configuration"]["matching_policy"], "seeds": rep["configuration"]["seeds"]},
            prompt_versions=sorted({m["prompt_version"] for m in rep["configuration"]["models"] if m["prompt_version"]}),
            model_versions=rep["configuration"]["models"], metrics=rep["metrics"], gates=rep["gates"], gate_summary=rep["gate_summary"], details=details,
            created_by=actor_id, started_at=started, completed_at=datetime.now(UTC),
        ))  # fmt: skip
        audit.record(s, Actor(actor_id, "system_admin" if actor_id else "system"), "evaluation.run", "evaluation_run", rep["run_id"], "success",
                     {"split": cfg.split, "dataset_version": cfg.dataset_version, "gate_summary": rep["gate_summary"], "metrics_digest": rep["metrics_digest"]})  # fmt: skip
        s.commit()
