"""Background job `caregaps.create_cases`: create care-gap cases from the latest eligibility evaluations (04 §12.4).

input_scope: rule_version_ids [uuid] (optional; default all), outcomes [eligible|requires_clinical_review|
insufficient_evidence] (default eligible + requires_clinical_review), auto_approve bool (default true).
Idempotent: evaluations that already have a case, and residents that already have an open case for the care activity,
are skipped, so a retried job never duplicates anything.
"""

from __future__ import annotations

import uuid

from anticipa.caregaps.service import CASE_OUTCOMES, create_cases_scan
from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.jobs.context import JobContext
from anticipa.jobs.registry import register_job

JOB_TYPE = "caregaps.create_cases"


@register_job(JOB_TYPE)
def handle(ctx: JobContext) -> None:
    scope = ctx.job.input_scope
    actor = Actor(ctx.job.created_by, "system")

    def on_chunk(counts: dict[str, int]) -> None:
        ctx.progress(processed=counts["scanned"], succeeded=counts["scanned"])
        ctx.checkpoint({"last_chunk": counts})
        ctx.raise_if_cancelled()

    totals = create_cases_scan(
        ctx.session,
        [uuid.UUID(x) for x in scope.get("rule_version_ids", [])] or None,
        outcomes=tuple(scope.get("outcomes", CASE_OUTCOMES)),
        auto_approve=bool(scope.get("auto_approve", True)),
        actor=actor,
        on_chunk=on_chunk,
    )
    with ctx.session() as s:
        audit.record(s, actor, "caregap.create_cases", "processing_job", ctx.job_id, "success", {"job_id": str(ctx.job_id), **totals})
        s.commit()
