"""Capacity planner: OR-Tools CP-SAT allocation of open care-gap cases to simulated clinic capacity (ADR-020).

Formulation (hard constraints are the registry in `validate.py`; every plan is re-checked there before it is stored)
---------------------------------------------------------------------------------------------------------------------
Stage 1 - CP-SAT over *groups*. Cases that are interchangeable for the optimisation share a group
`g = (service, district, priority, language)`; a *pool* `p` is one clinic service offering. Integer variable
`x[g,p]` = number of cases of group g served at pool p, created only for eligible pairs:
service match, clinic open + service active, travel tier(district(g), district(p)) <= max_travel_tier, and the pool has
free seats inside the planning horizon. Model size depends on groups x pools, not on the number of cases, which is
what makes 100k+ cases solvable in seconds.

    maximise   sum_{g,p} S*(W_g*1000 - travel_penalty*tier(g,p)) * x[g,p]          priority-weighted coverage
             + sum_{d in {district, language}} E * m_d                             equity (maximin)
    s.t.       sum_p x[g,p] <= |g|                       each case at most once
               sum_g x[g,p] <= free_seats(p)             slot capacity (sum of free seats in the horizon)
               z[d,k] * reach(d,k) <= 1e6 * served(d,k)  z = coverage ratio of group k of dimension d (ppm, floored)
               m_d <= z[d,k]                             for every group k with >= min_group_size cases
               E = round(equity_weight * V * S / 1e6),  V = sum_g W_g*1000*|g|   (equity is worth <= equity_weight*V*S per
                                                                                   dimension; S = integer scale on all coverage terms)

`reach(d,k)` counts only cases that have at least one eligible pool with capacity, so structural gaps (service not
offered, out of travel range) do not freeze the maximin term; they are reported as unallocated reasons instead.
Maximin (raise the worst-off group) is used rather than a spread penalty because it never "levels down" by idling seats.

Stage 2 - deterministic disaggregation per pool. Within each pool the cases chosen in stage 1 are ordered by
(priority weight desc, opened_at, case id) and given the earliest free slot that does not overlap another appointment
of the same resident (exact for any monotone "earlier is better for higher priority" preference). A final
work-conserving sweep places cases left over by the solver gap or by resident conflicts into any eligible free seat.

Reproducibility: fixed seed + single worker + recorded time limit/gap; model construction order is sorted; the input
snapshot hash covers cases, usable capacity and config.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ortools import __version__ as ORTOOLS_VERSION
from ortools.sat.python import cp_model

from anticipa.planning.domain import (
    ALGORITHM_VERSION,
    CONFIG_VERSION,
    RATIO_SCALE,
    VALUE_UNIT,
    Allocation,
    PlanConfig,
    PlannableCase,
    PlanResult,
    PoolSpec,
    SlotSpec,
    Unallocated,
    normalize_priority,
)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class PlanningError(RuntimeError):
    """The solver could not produce a plan (not a constraint violation: those are reported by `validate`)."""


class TravelModel:
    """Synthetic district geography (assumption pending Q-028): districts sit on a ring in code order and the travel
    tier is the ring distance. Tier 0 = own district, 1 = adjacent. ponytail: replace with a real distance matrix."""

    def __init__(self, districts: Sequence[str]) -> None:
        self.order = sorted(set(districts))
        self._idx = {d: i for i, d in enumerate(self.order)}
        self._n = len(self.order)

    @classmethod
    def from_inputs(cls, cases: Sequence[PlannableCase], pools: Sequence[PoolSpec]) -> TravelModel:
        return cls([c.district_code for c in cases if c.district_code] + [p.district_code for p in pools])

    def tier(self, a: str, b: str) -> int:
        d = abs(self._idx[a] - self._idx[b])
        return min(d, self._n - d)


@dataclass(slots=True)
class _Group:
    service: str
    district: str
    priority: str
    language: str
    weight: int
    cases: list[PlannableCase]
    pools: list[int]  # usable, travel-eligible pool indexes ordered by (tier, pool index)
    reason: str  # group-level reason when no seat is left (or none was reachable)
    n_offered: int
    n_eligible: int


def _opened(c: PlannableCase) -> datetime:
    return c.opened_at or EPOCH


def _usable(s: SlotSpec, p: PoolSpec, cfg: PlanConfig) -> bool:
    return (
        s.status == "open"
        and s.free > 0
        and p.clinic_open
        and p.service_active
        and cfg.horizon_start <= s.start
        and s.end <= cfg.horizon_end
        and s.end - s.start >= timedelta(minutes=p.duration_minutes)
    )


def _snapshot_hash(cases: Sequence[PlannableCase], pool_list: list[PoolSpec], usable: dict, cfg: PlanConfig) -> str:
    h = hashlib.sha256()
    h.update(json.dumps(cfg.model_dump(mode="json"), sort_keys=True).encode())
    for c in sorted(cases, key=lambda c: c.case_id.int):
        opened = c.opened_at.isoformat() if c.opened_at else ""
        h.update(f"c|{c.case_id}|{c.resident_id}|{c.service_key}|{c.priority}|{c.district_code}|{c.language}|{opened}\n".encode())
    for p in pool_list:
        h.update(f"p|{p.clinic_service_id}|{p.clinic_key}|{p.district_code}|{p.service_key}|{p.duration_minutes}\n".encode())
        for s in sorted(usable[p.clinic_service_id], key=lambda s: s.slot_id.int):
            h.update(f"s|{s.slot_id}|{s.start.isoformat()}|{s.end.isoformat()}|{s.free}\n".encode())
    return h.hexdigest()


def _solve(groups: list[_Group], cap: list[int], pool_list: list[PoolSpec], travel: TravelModel,
           demand_all: dict[str, dict[str, int]], cfg: PlanConfig) -> tuple[dict[tuple[int, int], int], dict[str, Any]]:  # fmt: skip
    model = cp_model.CpModel()
    total_value = sum(g.weight * VALUE_UNIT * len(g.cases) for g in groups if g.pools)
    # Scale coverage values up on small instances so the integer equity coefficient is accurate to ~1%.
    at_stake = cfg.equity_weight * total_value
    scale = max(1, math.ceil(100 * RATIO_SCALE / at_stake)) if at_stake > 0 else 1
    x: dict[tuple[int, int], cp_model.IntVar] = {}
    by_pool: dict[int, list[cp_model.IntVar]] = defaultdict(list)
    objective: list[Any] = []
    for gi, g in enumerate(groups):
        for pi in g.pools:
            v = model.NewIntVar(0, min(len(g.cases), cap[pi]), f"x_{gi}_{pi}")
            x[gi, pi] = v
            by_pool[pi].append(v)
            tier = travel.tier(g.district, pool_list[pi].district_code)
            objective.append(scale * max(1, g.weight * VALUE_UNIT - cfg.travel_penalty * tier) * v)
        if g.pools:
            model.Add(cp_model.LinearExpr.sum([x[gi, pi] for pi in g.pools]) <= len(g.cases))
    for pi in sorted(by_pool):
        model.Add(cp_model.LinearExpr.sum(by_pool[pi]) <= cap[pi])

    equity_dims: list[str] = []
    if at_stake > 0:
        coef = max(1, round(at_stake * scale / RATIO_SCALE))
        for dim in cfg.equity_dimensions:
            members: dict[str, list[int]] = defaultdict(list)
            for gi, g in enumerate(groups):
                if g.pools:
                    members[getattr(g, dim)].append(gi)
            keys = sorted(k for k in members if demand_all[dim][k] >= cfg.min_group_size)
            if len(keys) < 2:
                continue
            m = model.NewIntVar(0, RATIO_SCALE, f"m_{dim}")
            for k in keys:
                gis = members[k]
                reach = sum(len(groups[gi].cases) for gi in gis)
                served = cp_model.LinearExpr.sum([x[gi, pi] for gi in gis for pi in groups[gi].pools])
                z = model.NewIntVar(0, RATIO_SCALE, f"z_{dim}_{k}")
                model.Add(z * reach <= RATIO_SCALE * served)
                model.Add(m <= z)
            objective.append(coef * m)
            equity_dims.append(dim)
    model.Maximize(cp_model.LinearExpr.sum(objective))

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = cfg.seed % (2**31 - 1)
    solver.parameters.num_workers = cfg.num_workers
    solver.parameters.max_time_in_seconds = cfg.time_limit_seconds
    solver.parameters.relative_gap_limit = cfg.relative_gap
    solver.parameters.linearization_level = 2  # LP-heavy (coverage ratios + maximin): 10x faster to a 0.01% gap than level 1
    status = solver.Solve(model)
    name = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise PlanningError(f"CP-SAT returned {name}")
    counts = {key: solver.Value(v) for key, v in x.items() if solver.Value(v) > 0}
    obj, bound = solver.ObjectiveValue(), solver.BestObjectiveBound()
    return counts, {
        "status": name,
        "objective": obj,
        "best_bound": bound,
        "gap": abs(bound - obj) / max(1.0, abs(obj)),
        "wall_time_s": round(solver.WallTime(), 3),
        "variables": len(x),
        "groups": len(groups),
        "pools": len(by_pool),
        "equity_dimensions_modelled": equity_dims,
        "objective_scale": scale,
    }


def _equity_summary(rows: dict[str, dict[str, int]], min_group_size: int) -> dict[str, Any]:
    groups = []
    for k in sorted(rows):
        r = rows[k]
        cov = r["allocated"] / r["demand"] if r["demand"] else None
        groups.append({"group": k, **r, "coverage": cov, "small_group": r["demand"] < min_group_size})
    qualifying = [g["coverage"] for g in groups if not g["small_group"] and g["reachable"] > 0 and g["coverage"] is not None]
    out: dict[str, Any] = {"groups": groups, "compared_groups": len(qualifying)}
    if len(qualifying) >= 2:
        lo, hi = min(qualifying), max(qualifying)
        out |= {"min_coverage": lo, "max_coverage": hi, "gap": hi - lo, "ratio": (lo / hi) if hi else None}
    else:
        out["note"] = "fewer than two groups meet min_group_size: no comparison"
    return out


def plan(cases: Sequence[PlannableCase], pools: Sequence[PoolSpec], slots: Sequence[SlotSpec], cfg: PlanConfig) -> PlanResult:
    clock = time.perf_counter()
    cases = list(cases)
    pool_by_id = {p.clinic_service_id: p for p in pools}
    usable: dict[Any, list[SlotSpec]] = defaultdict(list)
    for s in slots:
        p = pool_by_id.get(s.clinic_service_id)
        if p is not None and _usable(s, p, cfg):
            usable[s.clinic_service_id].append(s)

    pool_list = sorted((p for p in pools if p.clinic_service_id in usable), key=lambda p: (p.clinic_key, p.service_key))
    pool_slots = [sorted(usable[p.clinic_service_id], key=lambda s: (s.start, s.slot_id.int)) for p in pool_list]
    cap = [sum(s.free for s in sl) for sl in pool_slots]
    travel = TravelModel.from_inputs(cases, pools)
    snapshot = _snapshot_hash(cases, pool_list, usable, cfg)

    usable_by_service: dict[str, list[int]] = defaultdict(list)
    for pi, p in enumerate(pool_list):
        usable_by_service[p.service_key].append(pi)
    offered_by_service: dict[str, list[PoolSpec]] = defaultdict(list)
    for p in pools:
        offered_by_service[p.service_key].append(p)

    # ---- group valid cases; set invalid ones aside
    unallocated: list[Unallocated] = []
    buckets: dict[tuple[str, str, str, str], list[PlannableCase]] = defaultdict(list)
    for c in cases:
        if not c.service_key or not c.district_code:
            unallocated.append(Unallocated(c.case_id, "invalid_case_data",
                "Case has no service or no district, so it cannot be matched to a clinic.", {}))  # fmt: skip
        else:
            buckets[c.service_key, c.district_code, normalize_priority(c.priority), c.language or "unknown"].append(c)
    groups: list[_Group] = []
    for (service, district, priority, language), members in sorted(buckets.items()):
        members.sort(key=lambda c: (_opened(c), c.case_id.int))
        offered = offered_by_service.get(service, [])
        elig_any = [p for p in offered if travel.tier(district, p.district_code) <= cfg.max_travel_tier]
        eligible = sorted(
            (pi for pi in usable_by_service.get(service, ()) if travel.tier(district, pool_list[pi].district_code) <= cfg.max_travel_tier),
            key=lambda pi: (travel.tier(district, pool_list[pi].district_code), pi),
        )  # fmt: skip
        reason = ("service_not_offered" if not offered else "outside_travel_eligibility" if not elig_any
                  else "no_capacity_in_horizon" if not eligible else "capacity_exhausted")  # fmt: skip
        groups.append(_Group(service, district, priority, language, cfg.weight_of(priority), members, eligible,
                             reason, len(offered), len(elig_any)))  # fmt: skip
    demand_all: dict[str, dict[str, int]] = {"district": Counter(), "language": Counter()}
    for g in groups:
        demand_all["district"][g.district] += len(g.cases)
        demand_all["language"][g.language] += len(g.cases)
    t_prepare = time.perf_counter() - clock

    # ---- stage 1: CP-SAT over groups
    clock = time.perf_counter()
    if any(g.pools for g in groups):
        counts, solver_stats = _solve(groups, cap, pool_list, travel, demand_all, cfg)
    else:
        counts, solver_stats = {}, {"status": "SKIPPED_NO_ELIGIBLE_PAIRS"}
    t_solve = time.perf_counter() - clock

    # ---- stage 2: disaggregate per pool
    clock = time.perf_counter()
    residents = Counter(c.resident_id for g in groups for c in g.cases)
    multi = {r for r, n in residents.items() if n > 1}
    busy: dict[Any, list[tuple[datetime, datetime]]] = defaultdict(list)
    free = [[s.free for s in sl] for sl in pool_slots]
    cursor = [0] * len(pool_list)
    pool_left = list(cap)
    allocations: list[Allocation] = []
    tier_counts: Counter[int] = Counter()

    def take(pi: int, resident: Any) -> SlotSpec | None:
        sl, fr, c = pool_slots[pi], free[pi], cursor[pi]
        while c < len(sl) and fr[c] == 0:
            c += 1
        cursor[pi] = c
        if resident not in multi:
            j = c if c < len(sl) else -1
        else:
            j = next((j for j in range(c, len(sl)) if fr[j] and not any(sl[j].start < be and bs < sl[j].end for bs, be in busy[resident])), -1)
        if j < 0:
            return None
        fr[j] -= 1
        pool_left[pi] -= 1
        if resident in multi:
            busy[resident].append((sl[j].start, sl[j].end))
        return sl[j]

    pool_cases: dict[int, list[tuple[int, datetime, int, PlannableCase, int]]] = defaultdict(list)
    pending: list[tuple[int, int, PlannableCase]] = []  # (group index, weight, case) for the sweep
    for gi, g in enumerate(groups):
        pos = 0
        for pi in g.pools:
            k = counts.get((gi, pi), 0)
            for c in g.cases[pos : pos + k]:
                pool_cases[pi].append((-g.weight, _opened(c), c.case_id.int, c, gi))
            pos += k
        pending.extend((gi, g.weight, c) for c in g.cases[pos:])
    conflicted: set[Any] = set()
    for pi in sorted(pool_cases):
        for _, _, _, c, gi in sorted(pool_cases[pi], key=lambda t: t[:3]):
            s = take(pi, c.resident_id)
            if s is None:
                pending.append((gi, groups[gi].weight, c))
            else:
                allocations.append(Allocation(c.case_id, s.slot_id))
                tier_counts[travel.tier(c.district_code, pool_list[pi].district_code)] += 1
    pending.sort(key=lambda t: (-t[1], _opened(t[2]), t[2].case_id.int))
    for gi, _, c in pending:  # work-conserving sweep
        placed = False
        for pi in groups[gi].pools:
            if pool_left[pi] > 0:
                s = take(pi, c.resident_id)
                if s is not None:
                    allocations.append(Allocation(c.case_id, s.slot_id))
                    tier_counts[travel.tier(c.district_code, pool_list[pi].district_code)] += 1
                    placed = True
                    break
        if not placed:
            if any(pool_left[pi] > 0 for pi in groups[gi].pools):
                conflicted.add(c.case_id)
            else:
                g = groups[gi]
                unallocated.append(_unallocated(c, g, cfg, cap, pool_left))
    for _, _, c in pending:
        if c.case_id in conflicted:
            unallocated.append(Unallocated(c.case_id, "resident_time_conflict",
                "Free capacity exists, but every eligible slot overlaps another appointment proposed for the same resident.",
                {"service_key": c.service_key, "priority": c.priority}))  # fmt: skip
    allocations.sort(key=lambda a: a.case_id.int)
    unallocated.sort(key=lambda u: u.case_id.int)
    t_assign = time.perf_counter() - clock

    metrics = _metrics(len(cases), groups, allocations, unallocated, pool_list, cap, pool_left, tier_counts, cfg)
    return PlanResult(
        allocations, unallocated, metrics,
        {**solver_stats, "algorithm_version": ALGORITHM_VERSION, "config_version": CONFIG_VERSION, "ortools_version": ORTOOLS_VERSION,
         "seed": cfg.seed, "num_workers": cfg.num_workers, "time_limit_seconds": cfg.time_limit_seconds,
         "relative_gap_limit": cfg.relative_gap, "linearization_level": 2,
         "timings_s": {"prepare": round(t_prepare, 3), "solve": round(t_solve, 3), "assign": round(t_assign, 3)}},
        snapshot, travel.order,
    )  # fmt: skip


def _unallocated(c: PlannableCase, g: _Group, cfg: PlanConfig, cap: list[int], left: list[int]) -> Unallocated:
    seats = sum(cap[pi] for pi in g.pools)
    used = sum(cap[pi] - left[pi] for pi in g.pools)
    details = {"service_key": g.service, "priority": g.priority, "district_code": g.district, "max_travel_tier": cfg.max_travel_tier,
               "clinics_offering_service": g.n_offered, "clinics_within_travel": g.n_eligible,
               "eligible_pools_with_capacity": len(g.pools), "eligible_free_seats": seats, "eligible_seats_allocated": used}  # fmt: skip
    text = {
        "service_not_offered": f"No clinic offers service '{g.service}'.",
        "outside_travel_eligibility": f"Service '{g.service}' is offered by {g.n_offered} clinic(s), none within travel tier "
            f"{cfg.max_travel_tier} of district {g.district}.",
        "no_capacity_in_horizon": f"{g.n_eligible} clinic(s) within travel tier {cfg.max_travel_tier} offer '{g.service}', but none has "
            "free open capacity inside the planning horizon.",
        "capacity_exhausted": f"All {seats} free seat(s) at the {len(g.pools)} eligible clinic(s) for '{g.service}' were allocated "
            f"({used} used); demand exceeded capacity and this case was not selected under the priority-weighted, equity-adjusted objective.",
    }[g.reason]  # fmt: skip
    return Unallocated(c.case_id, g.reason, text, details)


def _metrics(total: int, groups: list[_Group], allocations: list[Allocation], unallocated: list[Unallocated],
             pool_list: list[PoolSpec], cap: list[int], left: list[int], tier_counts: Counter, cfg: PlanConfig) -> dict[str, Any]:  # fmt: skip
    allocated_ids = {a.case_id for a in allocations}
    dims: dict[str, dict[str, dict[str, int]]] = {k: defaultdict(lambda: {"demand": 0, "reachable": 0, "allocated": 0})
                                                  for k in ("district", "language", "priority", "service")}  # fmt: skip
    weighted_total = weighted_alloc = 0
    for g in groups:
        n = len(g.cases)
        a = sum(1 for c in g.cases if c.case_id in allocated_ids)
        weighted_total += g.weight * n
        weighted_alloc += g.weight * a
        for dim in dims:
            row = dims[dim][getattr(g, dim)]
            row["demand"] += n
            row["reachable"] += n if g.pools else 0
            row["allocated"] += a
    seats_total, seats_used = sum(cap), sum(c - rest for c, rest in zip(cap, left, strict=True))
    seats_by_district: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    seats_by_service: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for p, c, rest in zip(pool_list, cap, left, strict=True):
        for acc, key in ((seats_by_district, p.district_code), (seats_by_service, p.service_key)):
            acc[key][0] += c
            acc[key][1] += c - rest

    def cov(r: dict[str, int]) -> float | None:
        return r["allocated"] / r["demand"] if r["demand"] else None

    def table(dim: str, seats: dict[str, list[int]] | None = None) -> dict[str, Any]:
        out = {}
        for k, r in sorted(dims[dim].items()):
            row = {**r, "coverage": cov(r)}
            if seats is not None:
                s = seats.get(k, [0, 0])
                row |= {"seats": s[0], "seats_allocated": s[1], "utilization": s[1] / s[0] if s[0] else None}
            out[k] = row
        return out

    valid = sum(len(g.cases) for g in groups)
    n_alloc = len(allocations)
    return {
        "cases_total": total, "cases_valid": valid, "allocated": n_alloc, "unallocated": len(unallocated),
        "coverage": n_alloc / total if total else None,
        "priority_weighted_coverage": weighted_alloc / weighted_total if weighted_total else None,
        "seats_total": seats_total, "seats_allocated": seats_used,
        "utilization": seats_used / seats_total if seats_total else None,
        "by_priority": table("priority"), "by_service": table("service", seats_by_service),
        "by_district": table("district", seats_by_district), "by_language": table("language"),
        "equity": {
            "basis": "allocated cases / plannable cases per group (planner optimises over reachable cases)",
            "min_group_size": cfg.min_group_size, "equity_weight": cfg.equity_weight,
            **{dim: _equity_summary(dims[dim], cfg.min_group_size) for dim in ("district", "language")},
        },
        "travel_tier_counts": {str(t): n for t, n in sorted(tier_counts.items())},
        "reason_counts": dict(sorted(Counter(u.reason_code for u in unallocated).items())),
        "simulated": True,
    }  # fmt: skip
