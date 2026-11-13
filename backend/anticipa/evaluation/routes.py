"""Evaluation API, mounted under /api/v1 (Docs/09 §10.3). AGGREGATE results only: metrics, gate results, coverage and the ids
of safety-critical cases. No endpoint returns ground-truth files, labels, expected values or document text (ADR-008).

  GET  /analytics/evaluation              latest run (observer-readable: `analytics.view`)
  GET  /analytics/evaluation/runs         paged run list (metadata + gate summary)
  GET  /analytics/evaluation/runs/{id}    one run in full
  POST /analytics/evaluation/runs         submit `evaluation.run` (needs `processing.execute`; dev split of a dataset only; the
                                          test split is evaluated by the CLI with --final so it is a logged, deliberate act)
Instruction: deliver a working, tested, demonstrable Anticipa MVP including the evaluation report.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from anticipa.api.deps import PageDep, SessionDep
from anticipa.api.envelope import ok, paged, paginate
from anticipa.core import audit
from anticipa.core.auth import CurrentUser, require_permission
from anticipa.core.errors import NotFound
from anticipa.core.rbac import ANALYTICS_VIEW, PROCESSING_EXECUTE
from anticipa.evaluation.models import EvaluationRun
from anticipa.evaluation.report import digest
from anticipa.jobs.service import submit_job

router = APIRouter(prefix="/analytics/evaluation", tags=["evaluation"])
ViewUser = Annotated[CurrentUser, Depends(require_permission(ANALYTICS_VIEW))]
ExecuteUser = Annotated[CurrentUser, Depends(require_permission(PROCESSING_EXECUTE))]
DATASET_RE = r"^syn-\d{8}-s\d+$"


class RunSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: str = Field(pattern=DATASET_RE)
    cohort_id: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9._-]{2,80}$")  # AI cohort manifest id in the document store
    bootstrap_resamples: int = Field(200, ge=0, le=2000)


def _summary(r: EvaluationRun) -> dict[str, Any]:
    return {
        "run_id": r.run_id, "status": r.status, "run_type": r.run_type, "dataset_version": r.dataset_version, "split": r.split,
        "cohort_id": r.cohort_id, "ground_truth_id": r.ground_truth_id, "gate_summary": r.gate_summary,
        "metrics_digest": r.metrics_digest, "started_at": r.started_at, "completed_at": r.completed_at, "synthetic_status": r.synthetic_status,
        "synthetic_banner": "Results are measured against planted ground truth on generated data. This is a software evaluation, not a clinical validation.",
    }  # fmt: skip


def _full(r: EvaluationRun) -> dict[str, Any]:
    """The report as the Command Center page reads it (field names of its EvaluationReport type)."""
    return {**_summary(r), "created_at": r.created_at, "dataset": {"dataset_version": r.dataset_version, "split": r.split, "cohort_id": r.cohort_id},
            "config": r.config, "prompt_versions": r.prompt_versions, "model_versions": r.model_versions,
            "metrics": r.metrics, "gates": r.gates, **r.details}  # fmt: skip


def pick_latest(session) -> EvaluationRun | None:
    """The held out final run if one exists, else the newest complete run."""
    final_first = (EvaluationRun.run_type == "test_final").desc()
    return session.execute(select(EvaluationRun).where(EvaluationRun.status == "complete")
                           .order_by(final_first, EvaluationRun.created_at.desc(), EvaluationRun.id.desc()).limit(1)).scalar_one_or_none()  # fmt: skip


@router.get("", summary="Final evaluation run if one exists, else the latest complete run (aggregate metrics, gates, coverage)")
def latest(request: Request, session: SessionDep, user: ViewUser):
    run = pick_latest(session)
    if run is None:
        raise NotFound("No evaluation run exists yet; run `python -m anticipa.evaluation run --dataset-version <V>`.")
    audit.record(session, user.actor, "evaluation.view", "evaluation_run", run.run_id, "success", {})
    session.commit()
    return ok(request, _full(run))


@router.get("/runs", summary="Evaluation runs, newest first (metadata and gate summary)")
def list_runs(request: Request, session: SessionDep, user: ViewUser, params: PageDep):
    rows, total = paginate(session, select(EvaluationRun).order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc()), params)
    return paged(request, [_summary(r) for r in rows], params, total)


@router.get("/runs/{run_id}", summary="One evaluation run in full")
def get_run(run_id: str, request: Request, session: SessionDep, user: ViewUser):
    run = session.execute(select(EvaluationRun).where(EvaluationRun.run_id == run_id)).scalar_one_or_none()
    if run is None:
        raise NotFound("Evaluation run not found.")
    audit.record(session, user.actor, "evaluation.view", "evaluation_run", run.run_id, "success", {})
    session.commit()
    return ok(request, _full(run))


@router.post("/runs", status_code=202, summary="Submit an evaluation run (async job; dev split)")
def submit_run(body: RunSubmit, request: Request, session: SessionDep, user: ExecuteUser):
    scope = {"dataset_version": body.dataset_version, "split": "dev", "final": False, "cohort_id": body.cohort_id,
             "bootstrap_resamples": body.bootstrap_resamples}  # fmt: skip
    submitted = submit_job(session, "evaluation.run", scope, f"evaluation.run:{digest(scope)}", user.id)
    audit.record(session, user.actor, "evaluation.run.submit", "processing_job", submitted.job.id, "success", {"split": "dev", "dataset_version": body.dataset_version})
    session.commit()
    return ok(request, {"job_id": submitted.job.id, "created": submitted.created, "status": submitted.job.status})
