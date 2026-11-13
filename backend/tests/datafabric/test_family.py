"""Family linker invariants on synthetic age/sex arrays (no database)."""
# ruff: noqa: E501, S311

from __future__ import annotations

import random
from collections import Counter
from datetime import date

import pytest

from anticipa.datafabric.family import YEAR, LinkerConfig, build_units, unit_edges

REF = date(2026, 11, 8).toordinal()
RECIPROCAL = {
    "mother": {"daughter", "son"}, "father": {"daughter", "son"}, "daughter": {"mother", "father"}, "son": {"mother", "father"},
    "sister": {"sister", "brother"}, "brother": {"sister", "brother"}, "spouse": {"spouse"},
    "maternal_grandmother": {"granddaughter", "grandson"}, "maternal_grandfather": {"granddaughter", "grandson"},
    "paternal_grandmother": {"granddaughter", "grandson"}, "paternal_grandfather": {"granddaughter", "grandson"},
    "granddaughter": {f"{s}_grand{p}" for s in ("maternal", "paternal") for p in ("mother", "father")},
    "grandson": {f"{s}_grand{p}" for s in ("maternal", "paternal") for p in ("mother", "father")},
    "maternal_aunt": {"niece", "nephew"}, "maternal_uncle": {"niece", "nephew"},
    "paternal_aunt": {"niece", "nephew"}, "paternal_uncle": {"niece", "nephew"},
    "niece": {f"{s}_{r}" for s in ("maternal", "paternal") for r in ("aunt", "uncle")},
    "nephew": {f"{s}_{r}" for s in ("maternal", "paternal") for r in ("aunt", "uncle")},
}  # fmt: skip


def population(n: int, seed: int = 1):
    rng = random.Random(seed)
    born = [REF - int(rng.uniform(0, 90) * YEAR) for _ in range(n)]
    sex = [rng.choice(["female", "male"]) for _ in range(n)]
    return born, sex


def build(n=3000, seed=1, rng_seed="d1", cfg=None):
    born, sex = population(n, seed)
    units = build_units(born, sex, REF, random.Random(rng_seed), cfg or LinkerConfig())
    return born, sex, units


def test_deterministic_for_same_seed_and_different_for_another():
    _, _, a = build()
    _, _, b = build()
    _, _, c = build(rng_seed="d2")
    assert a == b and a != c


def test_each_resident_in_at_most_one_unit_and_fraction_is_near_target():
    born, _, units = build()
    members = [m for u in units for m in u.members()]
    assert len(members) == len(set(members))
    assert all(len(u.members()) >= 2 for u in units)
    assert 0.55 <= len(members) / len(born) <= 0.75  # target 0.60, last unit may overshoot


def test_edges_are_valid_unique_and_reciprocal():
    born, sex, units = build()
    edges = [e for u in units for e in unit_edges(u, sex)]
    assert edges and all(a != b for a, b, *_ in edges)  # no self edges
    keys = Counter((a, b, t) for a, b, t, *_ in edges)
    assert max(keys.values()) == 1  # no duplicate (resident, related, type)
    by_pair = {(a, b): t for a, b, t, *_ in edges}
    for (a, b), t in by_pair.items():
        back = by_pair.get((b, a))
        assert back in RECIPROCAL[t], (t, back)  # reciprocal row exists with a compatible type
        if t in {"mother", "grandmother"} or t.endswith("grandmother"):
            assert sex[b] == "female"
        if t.endswith("father") or t == "father":
            assert sex[b] == "male"


def test_parent_child_age_gaps_are_plausible():
    born, sex, units = build(n=6000, seed=3)
    limits = {"mother": 42.01, "father": 55.01}  # LinkerConfig max parent age at the child's birth
    gaps = [
        (t, (born[a] - born[b]) / YEAR) for u in units for a, b, t, *_ in unit_edges(u, sex) if t in limits
    ]
    assert gaps
    assert all(
        18 - 0.01 <= g <= limits[t] for t, g in gaps
    )  # parent 18+ years older, within the fertility window


def test_degree_and_side_fields():
    _, sex, units = build()
    seen = {}
    for u in units:
        for _a, _b, t, degree, side in unit_edges(u, sex):
            seen[t] = (degree, side)
    assert (
        seen["spouse"] == (None, None)
        and seen["mother"] == (1, "maternal")
        and seen["father"] == (1, "paternal")
    )
    assert seen["sister"][0] == 1 and seen["niece"] == (2, None)
    assert seen["maternal_grandmother"] == (2, "maternal") and seen["paternal_uncle"] == (2, "paternal")


@pytest.mark.parametrize("fraction", [0.0, 0.2])
def test_family_fraction_is_respected(fraction):
    born, _, units = build(cfg=LinkerConfig(family_fraction=fraction))
    members = sum(len(u.members()) for u in units)
    assert members <= int(fraction * len(born)) + 8
    if fraction == 0:
        assert units == []


def test_residents_without_binary_sex_are_never_linked():
    born, sex = population(500)
    sex[:50] = ["unknown"] * 50
    units = build_units(born, sex, REF, random.Random("x"), LinkerConfig())
    linked = {m for u in units for m in u.members()}
    assert linked.isdisjoint(range(50))
