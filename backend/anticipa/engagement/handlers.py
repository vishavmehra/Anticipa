"""Background job `engagement.simulate`: play the engagement + closure loop for a cohort of planned cases with SIMULATED,
seed-deterministic outcomes (see `anticipa.engagement.simulate`).

input_scope: seed (int, default 1), limit (default 500), planning_run_id (default: latest completed run),
confirm_proposed (default true: confirms the run's proposed allocations first), case_ids, rates {...}.
One transaction per case; a failing case is recorded as a failed item and the job ends `partially_completed`. A retried
job skips cases it already handled and cases that moved on (they are no longer waiting for contact).
"""

from __future__ import annotations

from collections import Counter

from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.engagement.simulate import SimScope, select_cohort, simulate_case
from anticipa.jobs.context import JobContext, short_error
from anticipa.jobs.registry import register_job

JOB_TYPE = "engagement.simulate"
CHECKPOINT_EVERY = 25


@register_job(JOB_TYPE)
def handle(ctx: JobContext) -> None:
    scope = SimScope.model_validate(ctx.job.input_scope)
    actor = Actor(ctx.job.created_by, "system")  # system role: simulated procedure records are verified on arrival
    with ctx.session() as s:
        cohort = select_cohort(s, scope)
    ctx.progress(total=len(cohort))
    totals: Counter[str] = Counter()
    for n, (case_id, allocation_id) in enumerate(cohort, 1):
        item = ctx.item(str(case_id))
        if item.done:
            continue
        try:
            with ctx.session() as s:
                outcome = simulate_case(s, case_id, allocation_id, scope, actor, ctx.job_id)
                s.commit()
        except Exception as exc:  # noqa: BLE001  (isolate the case; the job reports partial completion)
            item.fail(short_error(exc))
            ctx.progress(processed=1, failed=1)
            totals["failed"] += 1
            continue
        if outcome is None:
            item.skip()
            totals["skipped"] += 1
        else:
            item.succeed()
            totals[outcome.final_status] += 1
        ctx.progress(processed=1, succeeded=1)
        if n % CHECKPOINT_EVERY == 0:
            ctx.checkpoint({"stage": "running", "seed": scope.seed, "processed": n, "results": dict(totals)})
            ctx.raise_if_cancelled()
    ctx.checkpoint({"stage": "done", "seed": scope.seed, "processed": len(cohort), "results": dict(totals)})
    with ctx.session() as s:
        audit.record(s, actor, "engagement.simulate", "processing_job", ctx.job_id, "success",
                     {"seed": scope.seed, "cohort": len(cohort), "results": dict(totals), "simulated": True})  # fmt: skip
        s.commit()
