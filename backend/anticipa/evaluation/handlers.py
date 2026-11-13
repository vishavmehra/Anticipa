"""Background job `evaluation.run` (Contract 17 §4). input_scope = RunConfig fields (dataset_version, split, cohort_manifest,
final, ...). Idempotent per config: the submitter derives the idempotency key from the config hash, and re-running the same
config is safe (a new aggregate row each time, nothing else is written). Instruction: deliver a working, tested,
demonstrable Anticipa MVP including the evaluation report.
"""

from __future__ import annotations

import uuid
from typing import Any

from anticipa.evaluation import runner
from anticipa.jobs.context import JobContext
from anticipa.jobs.registry import register_job


def config_from_scope(scope: dict[str, Any]) -> runner.RunConfig:
    """`cohort_id` names an AI cohort manifest in the document store (never an arbitrary path)."""
    allowed = {"dataset_version", "split", "final", "bootstrap_resamples", "seed", "probe_closure", "profile"}
    cfg = runner.RunConfig(**{k: v for k, v in scope.items() if k in allowed})
    if scope.get("cohort_id"):
        from anticipa.evidence.cohort import load_manifest
        from anticipa.evidence.service import get_store

        manifest = load_manifest(get_store(), scope["cohort_id"])
        cfg.cohort_id, cfg.resident_ids = manifest.cohort_id, [str(r) for r in manifest.resident_ids]
    return cfg


@register_job("evaluation.run")
def run_evaluation_job(ctx: JobContext) -> None:
    cfg = config_from_scope(ctx.job.input_scope)
    cfg.persist = True
    ctx.progress(total=1)
    actor = ctx.job.created_by
    rep = runner.run_evaluation(ctx.session, cfg, actor_id=uuid.UUID(str(actor)) if actor else None, progress=lambda m: ctx.checkpoint({"stage": m}))
    ctx.checkpoint({"stage": "done", "run_id": rep["run_id"], "gate_summary": rep["gate_summary"]})
    ctx.progress(processed=1, succeeded=1)
