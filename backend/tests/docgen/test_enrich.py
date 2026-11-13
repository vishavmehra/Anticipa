# ruff: noqa: E501
"""Enrichment (enrich.py): documented rates, determinism, real records win, structured vs note-only, ground truth."""

from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

import orjson
import pytest

from anticipa.datafabric.docgen import enrich as en
from anticipa.datafabric.docgen import generate as gen
from anticipa.datafabric.docgen.context import ENRICHMENT_SOURCE, Rec
from anticipa.datafabric.docgen.generate import build_resident
from anticipa.datafabric.docgen.groundtruth import FILES, SeedMismatch
from anticipa.datafabric.docgen.planner import age_on
from anticipa.datafabric.docstore import LocalDocumentStore
from tests.docgen.conftest import AS_OF, VERSION, make_context

SEED = 11
RULES = Path(__file__).resolve().parents[3] / "config" / "guidelines" / "rules_draft.json"


@pytest.fixture(scope="module")
def population():
    """12,000 residents with no structured screening history, ages 21-84, both sexes."""
    return [
        make_context(50_000 + i, "en", "female" if i % 2 else "male", 21 + (i * 7) % 64, records=False)
        for i in range(12_000)
    ]


@pytest.fixture(scope="module")
def plans(population):
    return [(c, en.plan_enrichment(c, SEED, VERSION, AS_OF)) for c in population]


def _rate(plans, family, ages, sex, excluded_concept):
    eligible = up = 0
    for c, items in plans:
        age = age_on(c.birth_date, AS_OF)
        if not (ages[0] <= age <= ages[1] and c.sex == sex):
            continue
        if any(
            i.concept.startswith(excluded_concept) or i.concept == excluded_concept
            for i in items
            if i.family == "exclusion"
        ):
            continue
        eligible += 1
        up += any(i.family == family and i.status == "up_to_date" for i in items)
    return up / eligible, eligible


def test_up_to_date_rates_match_the_documented_assumptions(plans):
    crc_m, n_m = _rate(plans, "crc", (45, 75), "male", "total_colectomy")
    crc_f, _ = _rate(plans, "crc", (45, 75), "female", "total_colectomy")
    mam, n = _rate(plans, "breast", (40, 74), "female", "bilateral_mastectomy")
    cer, n_c = _rate(plans, "cervical", (21, 65), "female", "hysterectomy")
    cer_t, _ = _rate(plans, "cervical", (21, 65), "female", "total_hysterectomy")
    assert n_m > 1500 and n > 1500 and n_c > 1500
    assert (
        abs(crc_m - en.RATES["up_to_date"]["crc"]) < 0.04
        and abs(crc_f - en.RATES["up_to_date"]["crc"]) < 0.04
    )
    assert abs(mam - en.RATES["up_to_date"]["mammography"]) < 0.04
    assert (
        abs(cer - en.RATES["up_to_date"]["cervical"]) < 0.04
        and abs(cer_t - en.RATES["up_to_date"]["cervical"]) < 0.04
    )
    # rates are not applied outside their ages or to the other sex
    for c, items in plans:
        age = age_on(c.birth_date, AS_OF)
        for i in items:
            if i.family == "crc":
                assert age >= 45
            if i.family in ("breast", "cervical", "exclusion") and i.concept in (
                "mammography",
                "pap_smear",
                "hpv_test",
                "hysterectomy",
                "total_hysterectomy",
                "bilateral_mastectomy",
            ):
                assert c.sex == "female"
            if i.family == "cervical":
                assert 21 <= age <= 65


def test_exclusions_remove_the_matching_screening_and_modes_split_as_documented(plans):
    excl, comp = Counter(), Counter()
    for _, items in plans:
        fams = {i.family for i in items}
        concepts = {i.concept for i in items}
        if concepts & {"hysterectomy", "total_hysterectomy"}:
            assert "cervical" not in fams
        if "total_colectomy" in concepts:
            assert "crc" not in fams
        if "bilateral_mastectomy" in concepts:
            assert "breast" not in fams
        for i in items:
            (excl if i.family == "exclusion" else comp)[i.mode] += 1
    assert abs(comp["note_only"] / sum(comp.values()) - en.RATES["note_only_share"]["completion"]) < 0.03
    assert (
        sum(excl.values()) > 200
        and abs(excl["note_only"] / sum(excl.values()) - en.RATES["note_only_share"]["exclusion"]) < 0.10
    )
    assert any(i.concept == "total_hysterectomy" for _, items in plans for i in items)
    assert any(i.concept == "hysterectomy" for _, items in plans for i in items)


def test_dates_follow_the_guideline_intervals(plans):
    for c, items in plans[:4000]:
        for i in items:
            assert c.birth_date < i.when <= AS_OF - timedelta(days=29)
            if i.family == "exclusion":
                continue
            years = (AS_OF - i.when).days / 365.25
            interval = en.RATES["interval_years"][i.concept]
            if i.status == "up_to_date":
                assert years <= interval + 0.01
            else:
                assert years > interval
            assert age_on(c.birth_date, i.when) >= en.RATES["min_age"][i.concept] - 1


def test_deterministic_and_real_records_win(population):
    c = population[1000]
    a = en.plan_enrichment(c, SEED, VERSION, AS_OF)
    assert a == en.plan_enrichment(c, SEED, VERSION, AS_OF)
    others = [en.plan_enrichment(c, s, VERSION, AS_OF) for s in range(40)]
    assert any(o != a for o in others)
    # a resident who already has a (Synthea) colonoscopy, mammogram, Pap or hysterectomy record is never re-enriched
    full = [x for x in population if age_on(x.birth_date, AS_OF) in range(46, 60) and x.sex == "female"][:300]
    for x in full:
        x.procedures = [
            Rec(
                "procedure",
                None,
                "73761001",
                "Colonoscopy",
                AS_OF - timedelta(days=900),
                None,
                "completed",
                "colonoscopy",
                {},
                "r1",
            ),
            Rec(
                "procedure",
                None,
                "71651007",
                "Mammography",
                AS_OF - timedelta(days=300),
                None,
                "completed",
                "mammography",
                {},
                "r2",
            ),
            Rec(
                "procedure",
                None,
                "x",
                "Hysterectomy",
                AS_OF - timedelta(days=3000),
                None,
                "completed",
                "hysterectomy",
                {},
                "r3",
            ),
        ]
        families = {i.family for i in en.plan_enrichment(x, SEED, VERSION, AS_OF)}
        assert not families & {"crc", "breast", "cervical"}
        x.procedures = []


def test_structured_rows_use_selector_codes_and_enrichment_markers(population):
    rules = json.loads(RULES.read_text(encoding="utf-8")) if RULES.exists() else None
    ctxs = [c for c in population if age_on(c.birth_date, AS_OF) >= 45][:3000]
    rows = []
    for c in ctxs:
        items = en.plan_enrichment(c, SEED, VERSION, AS_OF)
        rows += en.clinical_record_rows(c, items, VERSION)
    assert len(rows) > 800
    assert len({r["id"] for r in rows}) == len(rows)
    assert len({(r["resident_id"], r["resource_type"], r["source_resource_id"]) for r in rows}) == len(rows)
    for r in rows:
        assert r["source_system"] == ENRICHMENT_SOURCE == "anticipa_enrichment"
        assert r["source_reference"].startswith(f"enrichment://{VERSION}/{r['resident_id']}/")
        assert r["synthetic_status"] == "synthetic" and r["category"] in ("procedure", "observation")
        assert (
            r["value_json"]["enrichment"] is True and r["value_json"]["tag"] and r["value_json"]["fact_type"]
        )
        assert r["resource_type"] == ("Procedure" if r["category"] == "procedure" else "Observation")
        assert r["effective_start"].tzinfo is not None
    assert {r["code"] for r in rows} >= {"73761001", "57905-2", "24623002", "10524-7", "21440-3"}
    if rules is not None:  # every planted code is a code of an evidence selector the rule engine reads
        selector_codes = {c["code"] for s in rules["selector_library"].values() for c in s.get("codes", [])}
        assert {v[3] for v in en.CODES.values()} <= selector_codes


def test_build_resident_keeps_note_only_completions_out_of_the_structured_rows(population):
    seen_note_only = seen_structured = 0
    for c in population[:3000]:
        before = (list(c.procedures), dict(c.observations), list(c.note_only))
        out = build_resident(c, VERSION, SEED, AS_OF)
        again = build_resident(c, VERSION, SEED, AS_OF)
        assert (c.procedures, c.observations, c.note_only) == before  # the input context is not mutated
        assert [d.id for d in out.docs] == [d.id for d in again.docs]
        note_only = {e["concept"] for e in out.enrichment if e["mode"] == "note_only"}
        structured = {e["concept"] for e in out.enrichment if e["mode"] == "structured"}
        row_concepts = {r["value_json"]["fact_type"] for r in out.enrichment_rows}
        assert len(out.enrichment_rows) == len(structured)
        assert not row_concepts & {en.CODES[k][6] for k in note_only if k in en.CODES and k not in structured}
        text = "\n".join(d.text for d in out.docs if d.doc_type == "clinical_note")
        for e in out.enrichment:
            if e["mode"] == "note_only":
                seen_note_only += 1
                assert e["structured_record"] is False and e["source_reference"] is None
                label = next(f for f in out.facts if f["label_id"] == e["document_label_id"])
                assert label["value"]["structured_record_present"] is False and label["quote"]["text"] in text
                assert label["temporal"] == "historical" and label["assertion"] == "positive"
            else:
                seen_structured += 1
                assert e["structured_record"] is True and e["source_reference"].startswith("enrichment://")
    assert seen_note_only > 50 and seen_structured > 100


def test_generate_writes_enrichment_ground_truth_and_refuses_to_flip_the_flag(tmp_path, monkeypatch):
    contexts = [
        make_context(80_000 + i, "en", "female" if i % 2 else "male", 45 + i % 25, records=False)
        for i in range(150)
    ]

    def loader(dataset_version, *, limit=None, graph=None, as_of):
        yield contexts[: limit or len(contexts)]

    monkeypatch.setattr(gen, "contexts_from_synthea", loader)
    monkeypatch.setattr("anticipa.datafabric.synthea.load_manifest", lambda d: {"reference_date": "20261106"})
    store, gt = LocalDocumentStore(tmp_path / "docs"), tmp_path / "gt"
    report = gen.generate(VERSION, seed=SEED, source="synthea", store=store, gt_root=gt)
    lines = [orjson.loads(x) for x in (gt / VERSION / FILES["enrichment"]).read_bytes().splitlines()]
    assert len(lines) == report.enrichment_items > 100
    assert {x["schema_version"] for x in lines} == {"gt.enrichment.v1"} and all(
        x["synthetic_status"] == "synthetic" for x in lines
    )
    manifest = orjson.loads((gt / VERSION / "documents/docgen_manifest.json").read_bytes())
    assert (
        manifest["enrichment"]["enabled"] is True
        and manifest["enrichment"]["rates"]["up_to_date"]["crc"] == 0.6
    )
    assert "A-E1" in manifest["enrichment"]["text"]
    assert manifest["counts"]["enrichment_mode/structured"] + manifest["counts"][
        "enrichment_mode/note_only"
    ] == len(lines)
    with pytest.raises(SeedMismatch):
        gen.generate(VERSION, seed=SEED, source="synthea", store=store, gt_root=gt, enrich=False)
    # a run without enrichment produces no enrichment file and no enrichment labels
    off = gen.generate(
        VERSION, seed=SEED, source="synthea", store=store, gt_root=tmp_path / "gt-off", enrich=False
    )
    assert off.enrichment_items == 0 and not (tmp_path / "gt-off" / VERSION / FILES["enrichment"]).exists()
