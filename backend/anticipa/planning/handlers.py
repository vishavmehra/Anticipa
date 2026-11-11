"""Planning background jobs, registered for the worker and the API (`anticipa.core.modules`).

planning.run               input_scope {"planning_run_id": uuid}: plan, validate and persist one run (idempotent)
planning.generate_capacity input_scope = NetworkParams keys (seed, start, days, ...): SIMULATED clinics + slots, idempotent
"""

from __future__ import annotations

import uuid

from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.jobs.context import JobContext
from anticipa.jobs.registry import register_job
from anticipa.planning.service import execute_run
from anticipa.planning.synthetic import generate_network, network_params_from_scope, persist_network


@register_job("planning.run")
def run_planning(ctx: JobContext) -> None:
    execute_run(ctx.session, uuid.UUID(str(ctx.job.input_scope["planning_run_id"])), ctx)


@register_job("planning.generate_capacity")
def generate_capacity(ctx: JobContext) -> None:
    params = network_params_from_scope(ctx.job.input_scope)
    net = generate_network(params)
    ctx.progress(total=len(net.slots))
    with ctx.session() as s:
        inserted = persist_network(s, net)
        audit.record(s, Actor.system(), "planning.capacity.generate", "capacity_slots", None, "success",
                     {"seed": params.seed, "inserted": inserted, "total_seats": net.total_seats, "simulated": True})  # fmt: skip
        s.commit()
    ctx.progress(processed=len(net.slots), succeeded=len(net.slots))
