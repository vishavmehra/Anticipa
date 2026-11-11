"""Planner behaviour: hard constraints on randomized instances, explanations, priority, determinism, equity, scale."""

from __future__ import annotations

import random
import uuid
from collections import Counter
from datetime import timedelta

import pytest

from anticipa.planning.domain import REASON_CODES, Allocation
from anticipa.planning.solver import plan
from anticipa.planning.synthetic import NetworkParams, generate_cases, generate_network
from anticipa.planning.validate import HARD_CONSTRAINTS, validate_plan

SERVICES = ("svc_a", "svc_b", "svc_c")


def independent_violations(allocs, cases, pools, slots, cfg) -> list[str]:
    """Deliberately naive re-check (pairwise loops, own ring distance) that shares no code with validate.py."""
    cmap, smap, pmap = ({c.case_id: c for c in cases}, {s.slot_id: s for s in slots}, {p.clinic_service_id: p for p in pools})
    districts = sorted({c.district_code for c in cases if c.district_code} | {p.district_code for p in pools})
    out: list[str] = []
    per_slot: Counter = Counter()
    per_case: Counter = Counter()
    for a in allocs:
        c, s = cmap[a.case_id], smap[a.slot_id]
        p = pmap[s.clinic_service_id]
        per_slot[s.slot_id] += 1
        per_case[c.case_id] += 1
        gap = abs(districts.index(c.district_code) - districts.index(p.district_code))
        if min(gap, len(districts) - gap) > cfg.max_travel_tier:
            out.append("travel")
        if c.service_key != p.service_key:
            out.append("service")
        if not (s.status == "open" and p.clinic_open and p.service_active):
            out.append("closed")
        if not (cfg.horizon_start <= s.start and s.end <= cfg.horizon_end):
            out.append("horizon")
        if s.end - s.start < timedelta(minutes=p.duration_minutes):
            out.append("duration")
    out += ["capacity" for sid, n in per_slot.items() if n > smap[sid].free]
    out += ["twice" for n in per_case.values() if n > 1]
    placed = [(cmap[a.case_id].resident_id, smap[a.slot_id]) for a in allocs]
    for i, (r1, s1) in enumerate(placed):
        for r2, s2 in placed[i + 1 :]:
            if r1 == r2 and s1.start < s2.end and s2.start < s1.end:
                out.append("resident_overlap")
    return out


def random_instance(seed: int, f):
    rng = random.Random(seed)  # noqa: S311
    districts = [f"D{i:02d}" for i in range(1, rng.randint(2, 6))]
    pools = [f.pool(f"c{i}", rng.choice(districts), svc, minutes=rng.choice([15, 30]),
                    clinic_open=rng.random() > 0.1, service_active=rng.random() > 0.1)
             for i in range(rng.randint(1, 6)) for svc in rng.sample(SERVICES, rng.randint(1, 3))]  # fmt: skip
    slots = []
    for p in pools:
        for hour in rng.sample(range(6, 20), rng.randint(0, 8)):
            slots.append(f.slot(p, hour, rng.randint(0, 6), day=rng.choice([-1, 0, 1, 2, 3, 8]),
                                minutes=rng.choice([None, None, p.duration_minutes - 10]),
                                status="open" if rng.random() > 0.1 else "closed"))  # fmt: skip
    residents = max(1, rng.randint(3, 40))
    cases = [f.case(i, rng.choice((*SERVICES, "svc_missing")), rng.choice([*districts, ""]) if rng.random() > 0.9 else rng.choice(districts),
                    rng.choice(["urgent", "high", "medium", "routine", "weird", ""]), rng.choice(["en", "hi", "ar"]),
                    resident=rng.randrange(residents)) for i in range(rng.randint(1, 80))]  # fmt: skip
    cfg = f.config(max_travel_tier=rng.randint(0, 2), equity_weight=rng.choice([0, 0.2, 1.0]), min_group_size=rng.choice([1, 5, 30]),
                   seed=rng.randint(0, 99))  # fmt: skip
    return cases, pools, slots, cfg


@pytest.mark.parametrize("seed", range(40))
def test_randomized_instances_never_violate_hard_constraints(seed, f):
    cases, pools, slots, cfg = random_instance(seed, f)
    res = plan(cases, pools, slots, cfg)
    assert validate_plan(res.allocations, cases, pools, slots, cfg) == []
    assert independent_violations(res.allocations, cases, pools, slots, cfg) == []
    allocated = {a.case_id for a in res.allocations}
    unallocated = {u.case_id for u in res.unallocated}
    assert allocated.isdisjoint(unallocated) and allocated | unallocated == {c.case_id for c in cases}
    assert len(allocated) == len(res.allocations)
    assert all(u.reason_code in REASON_CODES and u.explanation for u in res.unallocated)
    assert res.metrics["allocated"] == len(allocated) and res.metrics["unallocated"] == len(unallocated)


def test_hard_constraint_registry_is_complete_and_every_check_can_fail(f):
    p, q = f.pool("a", "D01", "svc_a"), f.pool("b", "D02", "svc_b")
    far = f.pool("far", "D05", "svc_a")
    anchors = [f.pool(f"anchor{i}", f"D0{i}", "anchor") for i in (1, 2, 3, 4, 5, 6)]  # defines a 6-district ring
    s1, s_wrong, s_far = f.slot(p, 9, 1), f.slot(q, 9, 5), f.slot(far, 9, 5)
    s_closed = f.slot(p, 10, 5, status="closed")
    s_short = f.slot(p, 11, 5, minutes=10)
    s_out = f.slot(p, 12, 5, day=9)
    s_ov = f.slot(p, 13, 5)
    s_ov2 = f.slot(f.pool("a2", "D01", "svc_a"), 13, 5)  # same time, another clinic
    c1, c2, c3 = f.case(1, "svc_a", "D01"), f.case(2, "svc_a", "D01"), f.case(3, "svc_a", "D01", resident=1)
    cfg = f.config(max_travel_tier=1)
    pools = [p, q, far, f.pool("a2", "D01", "svc_a"), *anchors]
    slots = [s1, s_wrong, s_far, s_closed, s_short, s_out, s_ov, s_ov2]
    bad = {
        "case_in_scope": [Allocation(uuid.uuid4(), s1.slot_id)],
        "slot_exists": [Allocation(c1.case_id, uuid.uuid4())],
        "one_allocation_per_case": [Allocation(c1.case_id, s1.slot_id), Allocation(c1.case_id, s_ov.slot_id)],
        "slot_capacity": [Allocation(c1.case_id, s1.slot_id), Allocation(c2.case_id, s1.slot_id)],
        "service_match": [Allocation(c1.case_id, s_wrong.slot_id)],
        "slot_availability": [Allocation(c1.case_id, s_closed.slot_id)],
        "appointment_duration": [Allocation(c1.case_id, s_short.slot_id)],
        "planning_horizon": [Allocation(c1.case_id, s_out.slot_id)],
        "travel_eligibility": [Allocation(c1.case_id, s_far.slot_id)],
        "resident_time_conflict": [Allocation(c1.case_id, s_ov.slot_id), Allocation(c3.case_id, s_ov2.slot_id)],
    }
    assert set(bad) == set(HARD_CONSTRAINTS) and all(callable(c) for c in HARD_CONSTRAINTS.values())
    for name, allocs in bad.items():
        names = {v.constraint for v in validate_plan(allocs, [c1, c2, c3], pools, slots, cfg)}
        assert name in names, f"{name} validator did not fire"


def test_over_demand_is_explained_and_priority_wins(f):
    p = f.pool("c1", "D01", "svc_a")
    cases = [f.case(i, "svc_a", "D01", "routine") for i in range(5)] + [f.case(10 + i, "svc_a", "D01", "urgent") for i in range(2)]
    res = plan(cases, [p], [f.slot(p, 9, 2), f.slot(p, 10, 1)], f.config())
    placed = {a.case_id for a in res.allocations}
    assert len(placed) == 3
    assert {c.case_id for c in cases if c.priority == "urgent"} <= placed  # urgent before routine
    assert len(res.unallocated) == 4
    for u in res.unallocated:
        assert u.reason_code == "capacity_exhausted"
        assert "free seat" in u.explanation and u.details["eligible_free_seats"] == 3 and u.details["eligible_seats_allocated"] == 3
    assert res.metrics["reason_counts"] == {"capacity_exhausted": 4}
    assert res.metrics["utilization"] == 1.0


def test_infeasible_cases_get_specific_reason_codes(f):
    ring = [f.pool(f"anchor{i}", f"D0{i}", "anchor") for i in range(1, 7)]  # 6 districts on the ring
    near, far = f.pool("near", "D01", "svc_a"), f.pool("far", "D04", "svc_b")
    late = f.pool("late", "D01", "svc_c")
    x, y = f.pool("cx", "D01", "svc_x"), f.pool("cy", "D01", "svc_y")
    slots = [f.slot(near, 9, 5), f.slot(far, 9, 5), f.slot(late, 9, 5, day=9), f.slot(x, 10, 5), f.slot(y, 10, 5)]
    cases = [
        f.case(1, "svc_none", "D01"),  # nobody offers it
        f.case(2, "svc_b", "D01"),  # offered only 3 districts away
        f.case(3, "svc_c", "D01"),  # offered nearby, but only outside the horizon
        f.case(4, "svc_a", ""),  # invalid
        f.case(5, "svc_x", "D01", resident=9),
        f.case(6, "svc_y", "D01", resident=9),  # same resident, only an overlapping slot exists
        f.case(7, "svc_a", "D01"),  # fine
    ]
    res = plan(cases, [*ring, near, far, late, x, y], slots, f.config(max_travel_tier=1))
    reasons = {u.case_id: u.reason_code for u in res.unallocated}
    assert reasons[cases[0].case_id] == "service_not_offered"
    assert reasons[cases[1].case_id] == "outside_travel_eligibility"
    assert reasons[cases[2].case_id] == "no_capacity_in_horizon"
    assert reasons[cases[3].case_id] == "invalid_case_data"
    assert sorted(reasons[c.case_id] for c in cases[4:6] if c.case_id in reasons) == ["resident_time_conflict"]
    assert cases[6].case_id not in reasons
    assert {u.reason_code for u in res.unallocated} <= set(REASON_CODES)


def test_resident_gets_non_overlapping_slots_when_possible(f):
    x, y = f.pool("cx", "D01", "svc_x"), f.pool("cy", "D01", "svc_y")
    slots = [f.slot(x, 10, 1), f.slot(y, 10, 1), f.slot(y, 12, 1)]
    cases = [f.case(1, "svc_x", "D01", resident=5), f.case(2, "svc_y", "D01", resident=5)]
    cfg = f.config()
    res = plan(cases, [x, y], slots, cfg)
    assert len(res.allocations) == 2 and not res.unallocated
    assert validate_plan(res.allocations, cases, [x, y], slots, cfg) == []


@pytest.mark.parametrize("breakage", ["slot_closed", "clinic_closed", "service_inactive", "too_short", "outside_horizon", "no_free_seat"])
def test_unusable_capacity_is_never_allocated(breakage, f):
    kw = {"clinic_closed": {"clinic_open": False}, "service_inactive": {"service_active": False}}.get(breakage, {})
    p = f.pool("c1", "D01", "svc_a", **kw)
    skw = {"slot_closed": {"status": "closed"}, "too_short": {"minutes": 10}, "outside_horizon": {"day": 9}}.get(breakage, {})
    s = f.slot(p, 9, 0 if breakage == "no_free_seat" else 3, **skw)
    res = plan([f.case(1, "svc_a", "D01")], [p], [s], f.config())
    assert not res.allocations and res.unallocated[0].reason_code in ("no_capacity_in_horizon", "capacity_exhausted")


def test_determinism_under_recorded_config(f):
    net = generate_network(NetworkParams(seed=3, clinics_per_district=1, days=2, target_total_seats=400))
    cases = generate_cases(500, seed=3)
    cfg = f.config(seed=11)
    a = plan(cases, net.pools(), net.slot_specs(), cfg)
    b = plan(list(reversed(cases)), net.pools(), net.slot_specs(), cfg)  # input order must not matter
    assert a.allocations == b.allocations and a.unallocated == b.unallocated
    assert a.input_hash == b.input_hash and a.metrics == b.metrics
    assert plan(cases, net.pools(), net.slot_specs(), f.config(seed=12)).input_hash != a.input_hash  # config is in the hash


def test_equity_term_raises_the_worst_off_group(f):
    clinic = f.pool("c1", "D01", "svc_a")
    slots = [f.slot(clinic, 9 + h, 20, day=d) for h in range(5) for d in range(1)]  # 100 seats
    cases = [f.case(i, "svc_a", "D01", "routine") for i in range(100)] + [f.case(1000 + i, "svc_a", "D02", "urgent") for i in range(100)]
    pools = [clinic, f.pool("c2", "D02", "svc_other")]
    plain = plan(cases, pools, slots, f.config(equity_weight=0))
    fair = plan(cases, pools, slots, f.config(equity_weight=1.0))
    p, q = plain.metrics["equity"]["district"], fair.metrics["equity"]["district"]
    assert p["min_coverage"] == 0.0 and p["max_coverage"] == 1.0  # priority alone starves D01
    assert q["min_coverage"] >= 0.45 and q["gap"] < 0.1  # equity term balances the districts
    assert plain.metrics["allocated"] == fair.metrics["allocated"] == 100  # same seats used, only who gets them changes


def test_equity_metrics_match_the_plan_and_flag_small_groups(f):
    clinic = f.pool("c1", "D01", "svc_a")
    cases = [f.case(i, "svc_a", "D01", language="en") for i in range(40)] + [f.case(100 + i, "svc_a", "D01", language="hi") for i in range(5)]
    res = plan(cases, [clinic], [f.slot(clinic, 9, 30)], f.config(min_group_size=30))
    by_lang = {g["group"]: g for g in res.metrics["equity"]["language"]["groups"]}
    assert by_lang["hi"]["small_group"] and not by_lang["en"]["small_group"]
    assert res.metrics["equity"]["language"]["compared_groups"] == 1 and "note" in res.metrics["equity"]["language"]
    placed = {a.case_id for a in res.allocations}
    for lang, g in by_lang.items():
        demand = [c for c in cases if c.language == lang]
        assert g["demand"] == len(demand) and g["allocated"] == sum(c.case_id in placed for c in demand)
        assert g["coverage"] == pytest.approx(g["allocated"] / g["demand"])
    d = res.metrics["by_district"]["D01"]
    assert d["demand"] == 45 and d["allocated"] == 30 and d["seats"] == 30 and d["utilization"] == 1.0


def test_multi_district_equity_with_travel(f):
    ring = [f.pool(f"anchor{i}", f"D0{i}", "anchor") for i in range(1, 5)]
    a, b = f.pool("a", "D01", "svc_a"), f.pool("b", "D02", "svc_a")
    slots = [f.slot(a, 9, 40), f.slot(b, 9, 10)]
    cases = [f.case(i, "svc_a", "D01") for i in range(40)] + [f.case(100 + i, "svc_a", "D02") for i in range(40)]
    res = plan(cases, [*ring, a, b], slots, f.config(max_travel_tier=1, equity_weight=1.0))
    cov = {d: r["coverage"] for d, r in res.metrics["by_district"].items()}
    assert cov["D01"] == pytest.approx(cov["D02"], abs=0.05)  # D02 borrows adjacent D01 capacity
    assert res.metrics["travel_tier_counts"].get("1", 0) > 0


def test_10k_cases_smoke(f):
    net = generate_network(NetworkParams(days=3, target_total_seats=6000))
    cases = generate_cases(10_000)
    cfg = f.config()
    res = plan(cases, net.pools(), net.slot_specs(), cfg)
    assert res.solver["status"] in ("OPTIMAL", "FEASIBLE")
    assert validate_plan(res.allocations, cases, net.pools(), net.slot_specs(), cfg) == []
    assert len(res.allocations) + len(res.unallocated) == len(cases)
    by_reason = Counter(u.reason_code for u in res.unallocated)
    assert set(by_reason) <= set(REASON_CODES)
