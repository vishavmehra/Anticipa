"""Background jobs of the guidelines module (contract 17 §4): `guidelines.evaluate_population` (deterministic
population evaluation, persisted) and `guidelines.dry_run` (Guideline Studio, read-only; see dryrun.py).

`guidelines.evaluate_population`

input_scope (all optional except what the API resolves at submission):
  evaluation_date (ISO date)    explicit evaluation clock; resolved to today (UTC) by the API when omitted
  guideline_version_ids [uuid]  approved guideline versions to run (the API resolves "all approved" to ids)
  dataset_version str           restrict to one dataset
  storage_profile full|rules    restrict to residents stored with that profile (lean residents carry
                                `;profile=rules`); omitted = every resident
  resident_ids [uuid]           restrict to explicit residents
  mode incremental|full         incremental skips residents whose input hash is unchanged
  chunk_size int                residents per transaction (default 1000, max 5000)
  create_cases bool             after evaluation, create care-gap cases (caregaps module)
  auto_approve bool             with create_cases: eligible cases go to approved_for_planning (default true)
The checkpoint stores the keyset cursor, so a retried or requeued job continues where it stopped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.core.errors import ValidationFailed
from anticipa.datafabric.profiles import PROFILES
from anticipa.guidelines.dryrun import JOB_TYPE as DRY_RUN_JOB
from anticipa.guidelines.dryrun import DryRunScope, run_dry_run
from anticipa.guidelines.evaluator import ENGINE_VERSION
from anticipa.guidelines.population import (
    EvalScope,
    PopulationStats,
    count_residents,
    evaluate_population,
    resolve_guidelines,
)
from anticipa.jobs import service as jobs_service
from anticipa.jobs.context import JobContext
from anticipa.jobs.registry import register_job

JOB_TYPE = "guidelines.evaluate_population"


def submit_evaluation_job(
    session,
    *,
    evaluation_date: date | None = None,
    dataset_version: str | None = None,
    mode: str = "incremental",
    chunk_size: int = 1000,
    create_cases: bool = True,
    auto_approve: bool = True,
    idempotency_key: str | None = None,
    created_by: uuid.UUID | None = None,
    guideline_keys: list[str] | None = None,
    storage_profile: str | None = None,
):
    """Queue an evaluation of every APPROVED, effective guideline version (used by the CLI and operators; the
    HTTP route builds the same scope). Nothing runs inline: a worker executes it, checkpointed and resumable.
    Submitting again with mode=incremental evaluates only residents whose relevant inputs changed, for example
    after evidence extraction. `guideline_keys` restricts the run to those guidelines (each must have an
    approved effective version) and `storage_profile` to residents stored with that profile."""
    on = evaluation_date or datetime.now(UTC).date()
    if storage_profile is not None and storage_profile not in PROFILES:
        raise ValidationFailed(f"storage_profile must be one of {', '.join(PROFILES)}.")
    resolved = resolve_guidelines(session, on, guideline_keys=guideline_keys)
    if not resolved:
        raise ValidationFailed("No approved guideline version applies on the evaluation date.")
    scope: dict = {
        "evaluation_date": on.isoformat(),
        "guideline_version_ids": [str(g.guideline_version_id) for g in resolved],
        "mode": mode,
        "chunk_size": chunk_size,
        "create_cases": create_cases,
        "auto_approve": auto_approve,
    }
    if dataset_version:
        scope["dataset_version"] = dataset_version
    if storage_profile:
        scope["storage_profile"] = storage_profile
    result = jobs_service.submit_job(
        session, JOB_TYPE, scope, idempotency_key, created_by, config_version=ENGINE_VERSION
    )
    if result.created:
        audit.record(
            session,
            Actor(created_by, "system"),
            "eligibility.job_submit",
            "processing_job",
            result.job.id,
            "success",
            {k: scope[k] for k in ("guideline_version_ids", "evaluation_date", "mode")},
        )
    return result


def scope_from_input(scope: dict) -> EvalScope:
    try:
        on = (
            date.fromisoformat(scope["evaluation_date"])
            if scope.get("evaluation_date")
            else datetime.now(UTC).date()
        )
        return EvalScope(
            evaluation_date=on,
            guideline_version_ids=[uuid.UUID(x) for x in scope.get("guideline_version_ids", [])],
            dataset_version=scope.get("dataset_version"),
            resident_ids=[uuid.UUID(x) for x in scope["resident_ids"]]
            if scope.get("resident_ids") is not None
            else None,
            mode=scope.get("mode", "incremental"),
            chunk_size=int(scope.get("chunk_size", 1000)),
            storage_profile=scope.get("storage_profile"),
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ValidationFailed(f"Invalid evaluation job scope: {exc}") from exc


@register_job(DRY_RUN_JOB)
def handle_dry_run(ctx: JobContext) -> None:
    """Studio dry run: read only evaluation of one version on a sample; the result document is stored
    as the job checkpoint (`checkpoint["result"]`). Nothing is written to evaluations or cases (dryrun.py)."""
    scope = DryRunScope.from_input(ctx.job.input_scope)
    ctx.progress(  # a manual retry restarts from scratch: zero the counters first
        processed=-ctx.job.processed_items,
        succeeded=-ctx.job.succeeded_items,
        failed=-ctx.job.failed_items,
    )

    def on_chunk(residents: int) -> None:
        ctx.progress(processed=residents, succeeded=residents)
        ctx.raise_if_cancelled()

    result = run_dry_run(ctx.session, scope, on_start=lambda n: ctx.progress(total=n), on_chunk=on_chunk)
    ctx.checkpoint({"result": result})
    with ctx.session() as s:
        audit.record(
            s,
            Actor(ctx.job.created_by, "system"),
            "guideline.dry_run",
            "guideline_version",
            scope.version_id,
            "success",
            {
                "job_id": str(ctx.job_id),
                "sample": result["sample"],
                "outcomes": result["outcomes"],
                "newly_due": (result["diff"] or {}).get("newly_due"),
                "persisted": False,
            },
        )
        s.commit()


@register_job(JOB_TYPE)
def handle(ctx: JobContext) -> None:
    input_scope = ctx.job.input_scope
    scope = scope_from_input(input_scope)
    cp = ctx.job.checkpoint or {}
    resume_after = uuid.UUID(cp["last_resident_id"]) if cp.get("last_resident_id") else None
    if ctx.job.total_items is None:
        with ctx.session() as s:
            total = count_residents(s, scope)
        ctx.progress(total=total)

    def on_chunk(chunk: PopulationStats, total: PopulationStats) -> None:
        ctx.progress(
            processed=chunk.residents,
            succeeded=chunk.residents - chunk.residents_with_errors,
            failed=chunk.residents_with_errors,
        )
        prev = cp.get("totals", {})
        ctx.checkpoint(
            {
                "last_resident_id": str(chunk.last_resident_id),
                "totals": {
                    "evaluations_written": prev.get("evaluations_written", 0) + total.evaluations_written,
                    "skipped_unchanged": prev.get("skipped_unchanged", 0) + total.skipped_unchanged,
                },
            }
        )
        ctx.raise_if_cancelled()

    stats = evaluate_population(
        ctx.session, scope, job_id=ctx.job_id, resume_after=resume_after, on_chunk=on_chunk
    )
    summary: dict = {
        "job_id": str(ctx.job_id),
        "evaluation_date": scope.evaluation_date.isoformat(),
        **stats.as_dict(),
    }
    summary["guideline_version_ids"] = [str(x) for x in scope.guideline_version_ids]
    if input_scope.get("create_cases"):
        from anticipa.caregaps.service import (
            create_cases_scan,
            reconcile_open_cases,
        )  # lazy: caregaps depends on this module's tables

        created = create_cases_scan(
            ctx.session,
            scope.guideline_version_ids,
            auto_approve=bool(input_scope.get("auto_approve", True)),
            actor=Actor(ctx.job.created_by, "system"),
        )
        summary["cases"] = created
        summary["reconciled"] = reconcile_open_cases(
            ctx.session, scope.guideline_version_ids, actor=Actor(ctx.job.created_by, "system")
        )
    with ctx.session() as s:
        audit.record(
            s,
            Actor(ctx.job.created_by, "system"),
            "eligibility.evaluate_population",
            "processing_job",
            ctx.job_id,
            "success",
            summary,
        )
        s.commit()
