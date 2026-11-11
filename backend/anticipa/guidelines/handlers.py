"""Background job `guidelines.evaluate_population` (contract 17 §4): deterministic population evaluation.

input_scope (all optional except what the API resolves at submission):
  evaluation_date (ISO date)    explicit evaluation clock; resolved to today (UTC) by the API when omitted
  rule_version_ids [uuid]       approved rule versions to run (the API resolves "all approved" to explicit ids)
  dataset_version str           restrict to one dataset
  resident_ids [uuid]           restrict to explicit residents
  mode incremental|full         incremental skips residents whose input hash is unchanged
  chunk_size int                residents per transaction (default 1000, max 5000)
  create_cases bool             after evaluation, create care-gap cases (caregaps module)
  auto_approve bool             with create_cases: eligible cases go straight to approved_for_planning (default true)
The checkpoint stores the keyset cursor, so a retried or requeued job continues where it stopped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.core.errors import ValidationFailed
from anticipa.guidelines.population import EvalScope, PopulationStats, count_residents, evaluate_population
from anticipa.jobs.context import JobContext
from anticipa.jobs.registry import register_job

JOB_TYPE = "guidelines.evaluate_population"


def scope_from_input(scope: dict) -> EvalScope:
    try:
        on = date.fromisoformat(scope["evaluation_date"]) if scope.get("evaluation_date") else datetime.now(UTC).date()
        return EvalScope(
            evaluation_date=on,
            rule_version_ids=[uuid.UUID(x) for x in scope.get("rule_version_ids", [])],
            dataset_version=scope.get("dataset_version"),
            resident_ids=[uuid.UUID(x) for x in scope["resident_ids"]] if scope.get("resident_ids") is not None else None,
            mode=scope.get("mode", "incremental"),
            chunk_size=int(scope.get("chunk_size", 1000)),
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ValidationFailed(f"Invalid evaluation job scope: {exc}") from exc


@register_job(JOB_TYPE)
def handle(ctx: JobContext) -> None:
    input_scope = ctx.job.input_scope
    scope = scope_from_input(input_scope)
    cp = ctx.job.checkpoint or {}
    resume_after = uuid.UUID(cp["last_resident_id"]) if cp.get("last_resident_id") else None
    if ctx.job.total_items is None:
        with ctx.session() as s:
            ctx.progress(total=count_residents(s, scope))

    def on_chunk(chunk: PopulationStats, total: PopulationStats) -> None:
        ctx.progress(
            processed=chunk.residents, succeeded=chunk.residents - chunk.residents_with_errors,
            failed=chunk.residents_with_errors,
        )
        prev = cp.get("totals", {})
        ctx.checkpoint({
            "last_resident_id": str(chunk.last_resident_id),
            "totals": {
                "evaluations_written": prev.get("evaluations_written", 0) + total.evaluations_written,
                "skipped_unchanged": prev.get("skipped_unchanged", 0) + total.skipped_unchanged,
            },
        })  # fmt: skip
        ctx.raise_if_cancelled()

    stats = evaluate_population(
        ctx.session, scope, job_id=ctx.job_id, resume_after=resume_after, on_chunk=on_chunk
    )
    summary: dict = {"job_id": str(ctx.job_id), "evaluation_date": scope.evaluation_date.isoformat(), **stats.as_dict()}
    summary["rule_version_ids"] = [str(x) for x in scope.rule_version_ids]
    if input_scope.get("create_cases"):
        from anticipa.caregaps.service import create_cases_scan  # lazy: caregaps depends on this module's tables

        created = create_cases_scan(
            ctx.session, scope.rule_version_ids, auto_approve=bool(input_scope.get("auto_approve", True)),
            actor=Actor(ctx.job.created_by, "system"),
        )  # fmt: skip
        summary["cases"] = created
    with ctx.session() as s:
        audit.record(s, Actor(ctx.job.created_by, "system"), "eligibility.evaluate_population", "processing_job", ctx.job_id, "success", summary)
        s.commit()
