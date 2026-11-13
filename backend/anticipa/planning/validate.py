"""Hard-constraint registry and plan validator (answers Q-027 for the MVP; ADR-020: "validate every generated plan
before publication").

The validator re-derives every check from the *allocations and the capacity/case inputs only* - it shares no state with
the solver (it does reuse `TravelModel`, which is the definition of district geography, not solver logic). A plan with
any violation is never persisted. 09 §Evaluation re-runs these checks from persisted rows.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from anticipa.planning.domain import Allocation, PlanConfig, PlannableCase, PoolSpec, SlotSpec
from anticipa.planning.solver import TravelModel


@dataclass(frozen=True, slots=True)
class Violation:
    constraint: str
    case_id: uuid.UUID | None
    slot_id: uuid.UUID | None
    detail: str

    def as_dict(self) -> dict[str, str | None]:
        return {"constraint": self.constraint, "case_id": str(self.case_id) if self.case_id else None,
                "slot_id": str(self.slot_id) if self.slot_id else None, "detail": self.detail}  # fmt: skip


@dataclass
class PlanContext:
    cases: dict[uuid.UUID, PlannableCase]
    pools: dict[uuid.UUID, PoolSpec]  # by clinic_service_id
    slots: dict[uuid.UUID, SlotSpec]
    config: PlanConfig
    travel: TravelModel


Check = Callable[[Sequence[Allocation], PlanContext], list[Violation]]


def _resolve(a: Allocation, ctx: PlanContext) -> tuple[PlannableCase | None, SlotSpec | None, PoolSpec | None]:
    slot = ctx.slots.get(a.slot_id)
    return ctx.cases.get(a.case_id), slot, (ctx.pools.get(slot.clinic_service_id) if slot else None)


def case_in_scope(allocs: Sequence[Allocation], ctx: PlanContext) -> list[Violation]:
    return [Violation("case_in_scope", a.case_id, a.slot_id, "allocated case is not a plannable case of this run")
            for a in allocs if a.case_id not in ctx.cases]  # fmt: skip


def slot_exists(allocs: Sequence[Allocation], ctx: PlanContext) -> list[Violation]:
    return [Violation("slot_exists", a.case_id, a.slot_id, "slot or its clinic service does not exist")
            for a in allocs if _resolve(a, ctx)[2] is None]  # fmt: skip


def one_allocation_per_case(allocs: Sequence[Allocation], ctx: PlanContext) -> list[Violation]:
    n = Counter(a.case_id for a in allocs)
    return [Violation("one_allocation_per_case", cid, None, f"case allocated {k} times") for cid, k in n.items() if k > 1]


def slot_capacity(allocs: Sequence[Allocation], ctx: PlanContext) -> list[Violation]:
    used = Counter(a.slot_id for a in allocs)
    out = []
    for slot_id, k in used.items():
        slot = ctx.slots.get(slot_id)
        if slot is not None and k > max(slot.free, 0):
            out.append(Violation("slot_capacity", None, slot_id, f"{k} allocations exceed {slot.free} free seat(s)"))
    return out


def service_match(allocs: Sequence[Allocation], ctx: PlanContext) -> list[Violation]:
    out = []
    for a in allocs:
        case, _, pool = _resolve(a, ctx)
        if case and pool and case.service_key != pool.service_key:
            out.append(Violation("service_match", a.case_id, a.slot_id, f"case needs {case.service_key}, slot is {pool.service_key}"))
    return out


def slot_availability(allocs: Sequence[Allocation], ctx: PlanContext) -> list[Violation]:
    out = []
    for a in allocs:
        _, slot, pool = _resolve(a, ctx)
        if slot and pool and not (slot.status == "open" and pool.clinic_open and pool.service_active):
            out.append(Violation("slot_availability", a.case_id, a.slot_id, "slot not open, clinic closed or service inactive"))
    return out


def appointment_duration(allocs: Sequence[Allocation], ctx: PlanContext) -> list[Violation]:
    out = []
    for a in allocs:
        _, slot, pool = _resolve(a, ctx)
        if slot and pool and slot.end - slot.start < timedelta(minutes=pool.duration_minutes):
            out.append(Violation("appointment_duration", a.case_id, a.slot_id, "slot shorter than the service appointment duration"))
    return out


def planning_horizon(allocs: Sequence[Allocation], ctx: PlanContext) -> list[Violation]:
    cfg = ctx.config
    return [Violation("planning_horizon", a.case_id, a.slot_id, "slot outside the planning horizon")
            for a in allocs if (s := ctx.slots.get(a.slot_id)) and not (cfg.horizon_start <= s.start and s.end <= cfg.horizon_end)]  # fmt: skip


def travel_eligibility(allocs: Sequence[Allocation], ctx: PlanContext) -> list[Violation]:
    out = []
    for a in allocs:
        case, _, pool = _resolve(a, ctx)
        if case and pool and (
            not case.district_code or ctx.travel.tier(case.district_code, pool.district_code) > ctx.config.max_travel_tier
        ):
            out.append(Violation("travel_eligibility", a.case_id, a.slot_id,
                f"district {case.district_code} -> {pool.district_code} exceeds travel tier {ctx.config.max_travel_tier}"))  # fmt: skip
    return out


def resident_time_conflict(allocs: Sequence[Allocation], ctx: PlanContext) -> list[Violation]:
    by_resident: dict[uuid.UUID, list[tuple[SlotSpec, uuid.UUID]]] = defaultdict(list)
    for a in allocs:
        case, slot, _ = _resolve(a, ctx)
        if case and slot:
            by_resident[case.resident_id].append((slot, a.case_id))
    out = []
    for items in by_resident.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda t: t[0].start)
        latest_end, latest_case = items[0][0].end, items[0][1]
        for slot, case_id in items[1:]:
            if slot.start < latest_end:
                out.append(Violation("resident_time_conflict", case_id, slot.slot_id, f"overlaps appointment for case {latest_case}"))
            if slot.end > latest_end:
                latest_end, latest_case = slot.end, case_id
    return out


def consent_respected(allocs: Sequence[Allocation], ctx: PlanContext) -> list[Violation]:
    return [Violation("consent_respected", a.case_id, a.slot_id, "opted out of all outreach channels")
            for a in allocs if (c := ctx.cases.get(a.case_id)) and c.consent_denied]  # fmt: skip


# Q-027 answer for the MVP: the planner's declared hard constraints. Every entry has a validator by construction.
HARD_CONSTRAINTS: dict[str, Check] = {
    "case_in_scope": case_in_scope,
    "slot_exists": slot_exists,
    "one_allocation_per_case": one_allocation_per_case,
    "slot_capacity": slot_capacity,
    "service_match": service_match,
    "slot_availability": slot_availability,
    "appointment_duration": appointment_duration,
    "planning_horizon": planning_horizon,
    "travel_eligibility": travel_eligibility,
    "resident_time_conflict": resident_time_conflict,
    "consent_respected": consent_respected,
}


def validate_plan(allocations: Sequence[Allocation], cases: Sequence[PlannableCase], pools: Sequence[PoolSpec],
                  slots: Sequence[SlotSpec], config: PlanConfig) -> list[Violation]:  # fmt: skip
    ctx = PlanContext(
        cases={c.case_id: c for c in cases},
        pools={p.clinic_service_id: p for p in pools},
        slots={s.slot_id: s for s in slots},
        config=config,
        travel=TravelModel.from_inputs(cases, pools),
    )
    violations: list[Violation] = []
    for check in HARD_CONSTRAINTS.values():
        violations.extend(check(allocations, ctx))
    return violations
