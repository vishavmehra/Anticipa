"""Planning CLI (architect: mount in `anticipa.cli`, or run directly: `python -m anticipa.planning.cli <command>`).

  generate-capacity  simulated clinics + capacity slots into the database (idempotent, seeded)
  run                create a planning run and execute it inline (no worker needed), print the headline metrics
  benchmark          in-memory solver benchmark, see anticipa.planning.benchmark
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime

from anticipa.db import get_sessionmaker
from anticipa.planning import benchmark, service
from anticipa.planning.adapters import CaseFilters
from anticipa.planning.domain import PlanConfig
from anticipa.planning.synthetic import generate_network, network_params_from_scope, persist_network


def _dt(text: str) -> datetime:
    d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="anticipa-planning", description="SIMULATED capacity planning over synthetic residents")
    sub = ap.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate-capacity")
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--start", default="2026-11-10T00:00:00Z")
    g.add_argument("--days", type=int, default=7)
    g.add_argument("--clinics-per-district", type=int, default=3)
    g.add_argument("--target-total-seats", type=int)
    r = sub.add_parser("run")
    r.add_argument("--start", default="2026-11-10T00:00:00Z")
    r.add_argument("--end", default="2026-11-17T00:00:00Z")
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--max-travel-tier", type=int, default=1)
    r.add_argument("--equity-weight", type=float, default=0.2)
    b = sub.add_parser("benchmark")
    b.add_argument("sizes", type=int, nargs="*", default=[10_000, 100_000])
    args = ap.parse_args(argv)

    if args.command == "benchmark":
        benchmark.main([str(n) for n in args.sizes])
        return 0
    sessions = get_sessionmaker()
    if args.command == "generate-capacity":
        scope = {"seed": args.seed, "start": args.start, "days": args.days, "clinics_per_district": args.clinics_per_district}
        if args.target_total_seats:
            scope["target_total_seats"] = args.target_total_seats
        net = generate_network(network_params_from_scope(scope))
        with sessions() as s:
            inserted = persist_network(s, net)
            s.commit()
        print(json.dumps({"simulated": True, "generated_seats": net.total_seats, "inserted": inserted}))  # noqa: T201
        return 0
    cfg = PlanConfig(horizon_start=_dt(args.start), horizon_end=_dt(args.end), seed=args.seed,
                     max_travel_tier=args.max_travel_tier, equity_weight=args.equity_weight)  # fmt: skip
    run_id = uuid.uuid4()
    with sessions() as s:
        service.create_run(s, run_id=run_id, config=cfg, case_filters=CaseFilters(), created_by=None, job_id=None)
        s.commit()
    metrics = service.execute_run(sessions, run_id)
    keys = ("cases_total", "allocated", "unallocated", "coverage", "utilization", "reason_counts")
    print(json.dumps({"planning_run_id": str(run_id), "simulated": True, **{k: metrics[k] for k in keys}}))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
