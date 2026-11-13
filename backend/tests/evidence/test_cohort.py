"""The fixed AI cohort: deterministic, seeded, stratified selection and its persisted, verified manifest."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import date

import pytest

from anticipa.evidence.cohort import (
    MAX_COHORT_RESIDENTS,
    age_band,
    allocate,
    allocate_nested,
    cohort_key,
    default_cohort_id,
    docgen_difficult_residents,
    ids_sha256,
    load_manifest,
    save_manifest,
    select_cohort,
)


# ------------------------------------------------------------------------------------------------ unit
def test_allocation_is_proportional_exact_and_never_exceeds_a_stratum():
    sizes = {("a",): 600, ("b",): 250, ("c",): 150}
    assert allocate(sizes, 100) == {("a",): 60, ("b",): 25, ("c",): 15}
    out = allocate({("a",): 1, ("b",): 1, ("c",): 98}, 10)
    assert sum(out.values()) == 10 and out[("a",)] <= 1 and out[("b",)] <= 1
    assert allocate(sizes, 5000) == sizes  # more than exist: everything
    assert allocate({}, 3) == {} and allocate(sizes, 0) == dict.fromkeys(sizes, 0)
    assert allocate({("x",): 3, ("y",): 3}, 3) == allocate(
        {("y",): 3, ("x",): 3}, 3
    )  # ties never depend on order


def test_nested_allocation_keeps_language_proportions_exact_whatever_the_fine_strata_look_like():
    sizes = (
        {("en", f"b{i}"): 1 for i in range(60)}
        | {("hi", f"b{i}"): 1 for i in range(20)}
        | {("ar", f"b{i}"): 1 for i in range(10)}
    )
    out = allocate_nested(sizes, 30)  # 90 singleton strata: flat largest-remainder would be arbitrary here
    by_lang = Counter()
    for k, n in out.items():
        by_lang[k[0]] += n
    assert dict(by_lang) == {"en": 20, "hi": 7, "ar": 3} and all(n <= 1 for n in out.values())


def test_age_bands_and_ids():
    ref = date(2026, 11, 1)
    assert (
        age_band(date(2026, 11, 1), ref) == "0-17" and age_band(date(1976, 11, 2), ref) == "40-49"
    )  # birthday not yet
    assert age_band(date(1976, 11, 1), ref) == "50-59" and age_band(date(1930, 1, 1), ref) == "80+"
    assert age_band(None, ref) == "unknown"
    a, b = uuid.uuid4(), uuid.uuid4()
    assert ids_sha256([a, b]) == ids_sha256([b, a])
    assert default_cohort_id(7, 10000, "syn-20261108-s42") == "ai-syn-20261108-s42-s7-n10000"
    assert cohort_key("ai-x-s1-n5") == "cohorts/ai-x-s1-n5.json"
    for bad in ("../x", "A B", "x", "a/b"):
        with pytest.raises(ValueError):
            cohort_key(bad)
    assert MAX_COHORT_RESIDENTS == 10_000


def test_difficult_membership_comes_from_the_ground_truth_index_without_reading_labels(tmp_path):
    r1, r2, r3 = (uuid.uuid4() for _ in range(3))
    d = tmp_path / "syn-1" / "documents"
    d.mkdir(parents=True)
    lines = [
        {"resident_id": str(r1), "scenario_ids": ["DC-00"]},  # control only: not difficult
        {"resident_id": str(r2), "scenario_ids": ["DC-00", "DC-03"]},
        {"resident_id": str(r3), "scenario_ids": ["DC-07"], "quote": "never read"},
        {"resident_id": str(r1), "scenario_ids": None},
    ]
    (d / "document_index.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n\n", encoding="utf-8"
    )
    assert docgen_difficult_residents("syn-1", tmp_path) == {r2, r3}
    assert docgen_difficult_residents("missing", tmp_path) == set()


# ------------------------------------------------------------------------------------------ integration
class Store:
    def __init__(self):
        self.texts = {}

    def write_text(self, key, text):
        self.texts[key] = text
        return key

    def read_text(self, key):
        if key not in self.texts:
            raise FileNotFoundError(key)
        return self.texts[key]


@pytest.fixture
def population(world):
    """90 residents with documents (en 60 / hi 20 / ar 10, two sexes, three districts, mixed ages) + 5
    without."""
    rng = [("en", 60), ("hi", 20), ("ar", 10)]
    ids, i = [], 0
    for lang, n in rng:
        for _ in range(n):
            rid = world.resident(
                preferred_language=lang,
                administrative_sex="female" if i % 2 else "male",
                district_code=f"D{i % 3}",
                birth_date=date(1950 + (i * 7) % 60, 1 + i % 12, 15),
            )
            world.document(rid, f"Note {i}.")
            ids.append(rid)
            i += 1
    for _ in range(5):
        world.resident(preferred_language="en")  # no documents: never eligible
    return ids


pytestmark_integration = pytest.mark.integration


@pytest.mark.integration
def test_selection_is_deterministic_stratified_and_only_over_residents_with_documents(
    world, sessions, population
):
    dv = f"test-{world.tag}"
    with sessions() as s:
        a = select_cohort(s, seed=11, size=30, dataset_version=dv)
        b = select_cohort(s, seed=11, size=30, dataset_version=dv, now=a.created_at)
        c = select_cohort(s, seed=12, size=30, dataset_version=dv)
    assert (
        a.resident_ids == b.resident_ids and a.resident_ids_sha256 == b.resident_ids_sha256
    )  # same seed, same list
    assert a.resident_ids != c.resident_ids and len(set(a.resident_ids)) == a.size == 30
    assert set(a.resident_ids) <= set(population)  # residents without documents are never drawn
    assert a.distributions["language"] == {"ar": 3, "en": 20, "hi": 7}  # 60/20/10 of 90, proportionally
    assert sum(a.distributions["sex"].values()) == sum(a.distributions["district"].values()) == 30
    assert (
        set(a.distributions["district"]) == {"D0", "D1", "D2"}
        and a.strata >= 6
        and a.cohort_id == f"ai-{dv}-s11-n30"
    )
    assert (
        a.resident_ids_sha256 == ids_sha256(a.resident_ids) and a.target_size == 30 and a.difficult_count == 0
    )


@pytest.mark.integration
def test_difficult_case_residents_get_their_reserved_share_and_the_manifest_never_lists_them(
    world, sessions, population
):
    difficult = set(population[:12]) | set(population[70:75])  # 17 residents with planted difficult cases
    with sessions() as s:
        m = select_cohort(
            s,
            seed=3,
            size=20,
            dataset_version=f"test-{world.tag}",
            difficult_resident_ids=difficult,
            difficult_share=0.25,
        )
    in_cohort = len(set(m.resident_ids) & difficult)
    assert (
        m.difficult_count == in_cohort >= 5
    )  # round(20 x 0.25) are reserved; others may also fall in by chance
    summary = m.summary()
    assert "resident_ids" not in summary and "difficult_resident_ids" not in json.dumps(summary)
    assert summary["difficult_count"] == in_cohort


@pytest.mark.integration
def test_cohort_is_capped_and_small_populations_are_taken_whole(world, sessions, population):
    dv = f"test-{world.tag}"
    with sessions() as s:
        everyone = select_cohort(s, seed=1, size=10_000, dataset_version=dv)
        with pytest.raises(ValueError, match="fixed at 10000"):
            select_cohort(s, seed=1, size=10_001, dataset_version=dv)
        with pytest.raises(ValueError):
            select_cohort(s, seed=1, size=0, dataset_version=dv)
        none = select_cohort(s, seed=1, size=5, dataset_version=f"test-{world.tag}-nothing")
    assert (
        everyone.size == 90
        and set(everyone.resident_ids) == set(population)
        and everyone.target_size == 10_000
    )
    assert none.size == 0 and none.resident_ids == []


@pytest.mark.integration
def test_manifest_round_trips_and_a_tampered_one_is_rejected(world, sessions, population):
    store = Store()
    with sessions() as s:
        m = select_cohort(s, seed=5, size=15, dataset_version=f"test-{world.tag}")
    key = save_manifest(store, m)
    assert key == f"cohorts/{m.cohort_id}.json"
    loaded = load_manifest(store, m.cohort_id)
    assert (
        loaded.resident_ids == m.resident_ids
        and loaded.created_at == m.created_at
        and loaded.distributions == m.distributions
    )

    doc = json.loads(store.texts[key])
    doc["resident_ids"][0] = str(uuid.uuid4())  # swap one resident
    store.texts[key] = json.dumps(doc)
    with pytest.raises(ValueError, match="integrity"):
        load_manifest(store, m.cohort_id)
    doc["resident_ids"] = doc["resident_ids"][:-1]
    doc["resident_ids_sha256"] = ids_sha256(uuid.UUID(x) for x in doc["resident_ids"])
    store.texts[key] = json.dumps(doc)
    with pytest.raises(ValueError, match="invalid size"):
        load_manifest(store, m.cohort_id)
    with pytest.raises(FileNotFoundError):
        load_manifest(store, "ai-not-there-s1-n1")
    assert Counter(m.distributions["language"]).total() == 15
