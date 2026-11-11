"""In-memory planner benchmark (no database): `python -m anticipa.planning.benchmark 10000 100000`.

Prints one JSON object per size: instance size, solver status/gap, stage timings, peak memory, coverage, equity and the
hard-constraint violation count (must be 0). Synthetic cases + simulated capacity only.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

import psutil

from anticipa.planning.domain import PlanConfig
from anticipa.planning.solver import plan
from anticipa.planning.synthetic import NetworkParams, generate_cases, generate_network
from anticipa.planning.validate import validate_plan


def peak_rss_mb() -> float:
    info = psutil.Process().memory_info()
    peak = getattr(info, "peak_wset", None)  # Windows peak working set
    if peak is None:
        import resource  # POSIX: ru_maxrss is KiB on Linux

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    return round(peak / 2**20, 1)


def run_benchmark(n_cases: int, *, seed: int = 42, coverage_target: float = 0.6, num_workers: int = 1,
                  time_limit_seconds: float = 60.0, equity_weight: float = 0.2) -> dict[str, Any]:  # fmt: skip
    rss_start = round(psutil.Process().memory_info().rss / 2**20, 1)
    net = generate_network(NetworkParams(seed=seed, days=3 if n_cases < 20_000 else 7, target_total_seats=int(n_cases * coverage_target)))
    cases = generate_cases(n_cases, seed=seed)
    pools, slots = net.pools(), net.slot_specs()
    cfg = PlanConfig(horizon_start=datetime(2026, 11, 10, tzinfo=UTC), horizon_end=datetime(2026, 11, 17, tzinfo=UTC), seed=seed,
                     num_workers=num_workers, time_limit_seconds=time_limit_seconds, equity_weight=equity_weight)  # fmt: skip
    t = time.perf_counter()
    res = plan(cases, pools, slots, cfg)
    plan_s = time.perf_counter() - t
    t = time.perf_counter()
    violations = validate_plan(res.allocations, cases, pools, slots, cfg)
    validate_s = time.perf_counter() - t
    m, eq = res.metrics, res.metrics["equity"]
    return {
        "cases": n_cases, "slots": len(slots), "seats": m["seats_total"], "pools": len(pools),
        "solver": {k: res.solver[k] for k in ("status", "gap", "wall_time_s", "groups", "variables", "num_workers", "seed")},
        "timings_s": {**res.solver["timings_s"], "plan_total": round(plan_s, 2), "validate": round(validate_s, 2)},
        "memory_mb": {"rss_before": rss_start, "peak_rss": peak_rss_mb()},
        "allocated": m["allocated"], "unallocated": m["unallocated"], "coverage": round(m["coverage"], 4),
        "utilization": round(m["utilization"], 4), "reason_counts": m["reason_counts"],
        "equity": {d: {k: (round(eq[d][k], 4) if eq[d].get(k) is not None else None) for k in ("min_coverage", "max_coverage", "gap")}
                   for d in ("district", "language")},  # fmt: skip
        "hard_constraint_violations": len(violations),
    }


def main(argv: list[str] | None = None) -> None:
    sizes = [int(a) for a in (argv if argv is not None else sys.argv[1:])] or [10_000]
    for n in sizes:
        print(json.dumps(run_benchmark(n)), flush=True)  # noqa: T201


if __name__ == "__main__":
    main()
