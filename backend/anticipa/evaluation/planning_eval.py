"""Capacity-planner and equity metrics (Docs/09 §7.6 CP-*, §7.7 EQ-*) computed from PERSISTED rows.

Callers: anticipa.evaluation.runner. The validator below re-derives every hard-constraint check from the persisted
allocations and the capacity rows they point at; it shares no code with the solver (only the district-ring geography of
`TravelModel`, which defines "adjacent", is reused). It fails if the planner declares a constraint this file cannot
check. Capacity data is SIMULATED, so every figure here is about simulated capacity. Instruction: deliver a working,
tested, demonstrable Anticipa MVP including the evaluation report.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from anticipa.evaluation.stats import LOW_N, r6, rate

Row = Mapping[str, Any]
Check = Callable[[Sequence[Row], "Ctx"], list[str]]  # -> ids of offending allocations/slots


class Ctx:
    def __init__(self, start: datetime, end: datetime, max_tier: int, districts: Sequence[str],
                 existing_cases: set[Any] | None, allocations_total: int | None) -> None:  # fmt: skip
        from anticipa.planning.solver import TravelModel  # geography only (what "adjacent district" means)

        self.start, self.end, self.max_tier = start, end, max_tier
        self.travel = TravelModel([d for d in districts if d])
        self.existing_cases = existing_cases
        self.allocations_total = allocations_total


def _ids(rows: Sequence[Row]) -> list[str]:
    return [str(r["id"]) for r in rows]


def case_in_scope(rows: Sequence[Row], c: Ctx) -> list[str]:
    return [] if c.existing_cases is None else _ids([r for r in rows if r["care_gap_case_id"] not in c.existing_cases])


def slot_exists(rows: Sequence[Row], c: Ctx) -> list[str]:
    missing = (c.allocations_total or len(rows)) - len(rows)  # allocations whose slot/service/clinic row is gone
    return [f"missing-slot-{i}" for i in range(max(0, missing))]


def one_allocation_per_case(rows: Sequence[Row], c: Ctx) -> list[str]:
    n = Counter(r["care_gap_case_id"] for r in rows)
    return [str(k) for k, v in n.items() if v > 1]


def slot_capacity(rows: Sequence[Row], c: Ctx) -> list[str]:
    used = Counter(r["capacity_slot_id"] for r in rows)
    avail = {r["capacity_slot_id"]: r["available_capacity"] for r in rows}
    return [str(s) for s, k in used.items() if k > max(avail[s], 0)]


def service_match(rows: Sequence[Row], c: Ctx) -> list[str]:
    return _ids([r for r in rows if r["service_key"] != r["slot_service_key"]])


def slot_availability(rows: Sequence[Row], c: Ctx) -> list[str]:
    return _ids([r for r in rows if not (r["slot_status"] == "open" and r["clinic_status"] == "open" and r["service_active"])])


def appointment_duration(rows: Sequence[Row], c: Ctx) -> list[str]:
    return _ids([r for r in rows if r["slot_end"] - r["slot_start"] < timedelta(minutes=r["duration_minutes"])])


def planning_horizon(rows: Sequence[Row], c: Ctx) -> list[str]:
    return _ids([r for r in rows if not (c.start <= r["slot_start"] and r["slot_end"] <= c.end)])


def travel_eligibility(rows: Sequence[Row], c: Ctx) -> list[str]:
    return _ids([r for r in rows if not r["district_code"] or c.travel.tier(r["district_code"], r["clinic_district"]) > c.max_tier])


def resident_time_conflict(rows: Sequence[Row], c: Ctx) -> list[str]:
    by: dict[Any, list[Row]] = defaultdict(list)
    for r in rows:
        by[r["resident_id"]].append(r)
    bad: list[str] = []
    for items in by.values():
        items.sort(key=lambda r: r["slot_start"])
        latest_end = items[0]["slot_end"]
        for r in items[1:]:
            if r["slot_start"] < latest_end:
                bad.append(str(r["id"]))
            latest_end = max(latest_end, r["slot_end"])
    return bad


def consent_respected(rows: Sequence[Row], c: Ctx) -> list[str]:
    """No allocation for a resident who had opted out of every outreach channel at run end (ADR-070)."""
    return _ids([r for r in rows if r.get("consent_denied")])


def snapshot_matches_slot(rows: Sequence[Row], c: Ctx) -> list[str]:
    """Added check: the allocation's proposed times equal the slot it points at."""
    return _ids([r for r in rows if (r["proposed_start_time"], r["proposed_end_time"]) != (r["slot_start"], r["slot_end"])])


VALIDATORS: dict[str, Check] = {
    "case_in_scope": case_in_scope, "slot_exists": slot_exists, "one_allocation_per_case": one_allocation_per_case,
    "slot_capacity": slot_capacity, "service_match": service_match, "slot_availability": slot_availability,
    "appointment_duration": appointment_duration, "planning_horizon": planning_horizon,
    "travel_eligibility": travel_eligibility, "resident_time_conflict": resident_time_conflict,
    "snapshot_matches_slot": snapshot_matches_slot, "consent_respected": consent_respected,
}  # fmt: skip


def validate(rows: Sequence[Row], ctx: Ctx) -> dict[str, list[str]]:
    return {name: check(rows, ctx) for name, check in VALIDATORS.items()}


def declared_constraints() -> list[str]:
    from anticipa.planning.validate import HARD_CONSTRAINTS

    return list(HARD_CONSTRAINTS)


def evaluate_run(
    run: Row, rows: Mapping[str, Sequence[Row]], capacity: Mapping[str, Mapping[str, int]],
    *, existing_cases: set[Any] | None, allocations_total: int | None, reason_codes: Sequence[str],
) -> dict[str, Any]:
    """Metrics of ONE persisted planning run."""
    allocs, unalloc = rows["allocations"], rows["unallocated"]
    cfg = run["config"] or {}
    districts = [r["district_code"] for r in allocs] + [r["clinic_district"] for r in allocs] + [r["district_code"] for r in unalloc] + list(capacity)
    ctx = Ctx(run["planning_period_start"], run["planning_period_end"], int(cfg.get("max_travel_tier", 1)), districts,
              existing_cases, allocations_total)  # fmt: skip
    violations = validate(allocs, ctx)
    seats = sum(v["seats"] for v in capacity.values())
    seat_minutes = sum(v["seat_minutes"] for v in capacity.values())
    explained = sum(1 for u in unalloc if u["reason_code"] in reason_codes and (u["explanation"] or "").strip())
    hc = (run["metrics"] or {}).get("hard_constraints") or {}
    result: dict[str, Any] = {
        "planning_run_id": str(run["id"]), "allocations": len(allocs), "unallocated": len(unalloc),
        "violations_by_constraint": {k: len(v) for k, v in violations.items()},
        "violation_examples": {k: v[:5] for k, v in violations.items() if v},
        "slot_utilisation": rate(len(allocs), seats),
        "time_utilisation": rate(sum(r["duration_minutes"] for r in allocs), seat_minutes),
        "allocation_rate": rate(len(allocs), len(allocs) + len(unalloc)),
        "unallocated_explained": rate(explained, len(unalloc)),
        "unallocated_by_reason": dict(Counter(u["reason_code"] for u in unalloc)),
        "planner_recorded_validation": bool(hc.get("checked")) and hc.get("violations") == 0,
        "solver_status": (run["solver_parameters"] or {}).get("status"), "num_workers": cfg.get("num_workers"),
        "timings_s": (run["metrics"] or {}).get("timings_s"),
    }  # fmt: skip
    result["equity"] = equity(allocs, unalloc, run["planning_period_start"], capacity)
    return result


def equity(allocs: Sequence[Row], unalloc: Sequence[Row], start: datetime, capacity: Mapping[str, Mapping[str, int]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dim in ("district_code", "language"):
        got: Counter = Counter(r[dim] or "unknown" for r in allocs)
        left: Counter = Counter(r[dim] or "unknown" for r in unalloc)
        waits: dict[str, list[float]] = defaultdict(list)
        for r in allocs:
            waits[r[dim] or "unknown"].append((r["proposed_start_time"] - start).total_seconds() / 86400)
        groups = {g: rate(got[g], got[g] + left[g]) for g in sorted(set(got) | set(left))}
        usable = [g["value"] for g in groups.values() if g["value"] is not None and g["denominator"] >= LOW_N]
        out[dim] = {
            "allocation_rate_by_group": groups,
            "parity_gap_points": r6((max(usable) - min(usable)) * 100) if len(usable) >= 2 else None,
            "parity_ratio": r6(min(usable) / max(usable)) if len(usable) >= 2 and max(usable) > 0 else None,
            "groups_used": len(usable), "median_wait_days": {g: r6(statistics.median(v)) for g, v in sorted(waits.items())},
        }  # fmt: skip
    demand: Counter = Counter(r["district_code"] or "unknown" for r in [*allocs, *unalloc])
    out["capacity_to_demand_by_district"] = {
        d: {"seats": capacity.get(d, {}).get("seats", 0), "candidate_cases": n,
            "ratio": r6(capacity.get(d, {}).get("seats", 0) / n) if n else None}
        for d, n in sorted(demand.items())
    }  # fmt: skip
    return out


def aggregate(runs: Sequence[Mapping[str, Any]], declared: Sequence[str]) -> dict[str, Any]:
    """Cross-run metrics: CP-01 (sum, must be 0), CP-04, CP-09 and the registry check."""
    if not runs:
        return {"status": "not_evaluated", "reason": "no completed planning run exists"}
    by: Counter = Counter()
    for r in runs:
        by.update(r["violations_by_constraint"])
    uncovered = sorted(set(declared) - set(VALIDATORS))
    total_un = sum(r["unallocated"] for r in runs)
    expl = sum(r["unallocated_explained"]["numerator"] for r in runs)
    return {
        "runs_evaluated": len(runs),
        "CP-01": {"value": sum(by.values()), "unit": "count", "status": "measured", "n": len(runs), "by_constraint": dict(sorted(by.items())),
                  "metric": "hard-constraint violations across all evaluated plans (must be 0), independent validator",
                  "constraints_checked": sorted(VALIDATORS), "planner_declared_constraints": list(declared),
                  "declared_constraints_without_validator": uncovered},  # fmt: skip
        "CP-04": {**rate(expl, total_un), "metric": "unallocated cases with a valid reason code and explanation"},
        "CP-03": {"metric": "allocation rate (latest run)", **runs[0]["allocation_rate"]},
        "CP-02a": {"metric": "slot utilisation: allocated seats / available open seats in the horizon (latest run, SIMULATED capacity)", **runs[0]["slot_utilisation"]},
        "CP-02b": {"metric": "time utilisation: allocated appointment-minutes / available seat-minutes (latest run, SIMULATED)", **runs[0]["time_utilisation"]},
        "CP-07": {"metric": "solver status per run", "statuses": dict(Counter(r["solver_status"] or "unknown" for r in runs)), "status": "measured", "value": len(runs), "unit": "runs"},
        "CP-09": {"value": sum(1 for r in runs if not r["planner_recorded_validation"]), "unit": "count", "status": "measured", "n": len(runs),
                  "metric": "completed plans without a recorded passing hard-constraint validation (must be 0)"},  # fmt: skip
        "CP-05": {"value": None, "status": "not_evaluated", "reason": "needs a re-run of the solver on identical input; not executed by the evaluator"},
        "CP-06": {"value": None, "status": "not_evaluated", "reason": "no gt.planner_scenario ground truth exists (feasible/infeasible scenarios)"},
        "CP-08": {"value": None, "status": "not_evaluated", "reason": "no greedy baseline is computed"},
    }
