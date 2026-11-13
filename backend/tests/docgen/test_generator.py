# ruff: noqa: E501
"""Generator: planted quotes match offsets, determinism, languages, NFC, quotas, scenarios, isolation, resumability."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

import orjson
import pytest

from anticipa.datafabric.docgen import generate as gen
from anticipa.datafabric.docgen import phrases
from anticipa.datafabric.docgen.generate import build_resident
from anticipa.datafabric.docgen.groundtruth import FILES, FLOORS, SeedMismatch, verify_ground_truth
from anticipa.datafabric.docgen.planner import Item, assign_scenarios, plan_resident
from anticipa.datafabric.docstore import LocalDocumentStore
from anticipa.evidence.quotes import verify_quote
from tests.docgen.conftest import AS_OF, VERSION, make_context

SEED = 7
DEVANAGARI = re.compile("[ऀ-ॿ]")
ARABIC = re.compile("[؀-ۿ]")
RELATIONSHIPS = set(phrases.RELATIONS) | {"other_relative"}


@pytest.fixture
def outputs(contexts):
    return [build_resident(c, VERSION, SEED, AS_OF) for c in contexts]


def _docs(outputs) -> dict:
    return {str(d.id): d for o in outputs for d in o.docs}


def _facts(outputs) -> list[dict]:
    return [f for o in outputs for f in o.facts]


def test_every_planted_quote_equals_the_stored_text_slice(outputs):
    docs, checked = _docs(outputs), 0
    for f in _facts(outputs):
        if f["document_id"] is None:
            assert f["quote"] is None and f["expected_present"] is False
            continue
        doc = docs[f["document_id"]]
        assert f["document_content_sha256"] == doc.hash and f["language"] == doc.language
        q = f["quote"]
        if q is None:
            continue
        checked += 1
        assert q["offset_basis"] == "original_codepoints" and 0 <= q["start"] < q["end"] <= len(doc.text)
        assert doc.text[q["start"] : q["end"]] == q["text"] != ""
        for span in f["acceptable_quote_spans"]:
            assert span["start"] <= q["start"] and q["end"] <= span["end"] <= len(doc.text)
        v = verify_quote(
            doc.text, q["text"], expected_span=(q["start"], q["end"])
        )  # the pipeline's own verifier agrees
        assert v.status == "verified_exact", (v, q["text"])
    assert checked > 1000


def test_injected_spans_are_inside_the_document_and_carry_no_fact_quote(outputs):
    docs, found = _docs(outputs), 0
    for f in _facts(outputs):
        if f["injected_span"]:
            found += 1
            s, doc = f["injected_span"], docs[f["document_id"]]
            assert 0 <= s["start"] < s["end"] <= len(doc.text)
            assert f["quote"] is None and f["expected_present"] is False and "DC-15" in f["scenario_ids"]
    assert found >= 10


def test_deterministic_by_seed(contexts):
    sample = contexts[:120]
    a = [build_resident(c, VERSION, SEED, AS_OF) for c in sample]
    b = [build_resident(c, VERSION, SEED, AS_OF) for c in sample]
    other = [build_resident(c, VERSION, SEED + 1, AS_OF) for c in sample]
    assert [d.text for o in a for d in o.docs] == [d.text for o in b for d in o.docs]
    assert [orjson.dumps(f, option=orjson.OPT_SORT_KEYS) for o in a for f in o.facts] == [
        orjson.dumps(f, option=orjson.OPT_SORT_KEYS) for o in b for f in o.facts
    ]
    assert [d.id for o in a for d in o.docs] == [d.id for o in b for d in o.docs]
    assert [d.text for o in a for d in o.docs] != [d.text for o in other for d in o.docs]


def test_language_coverage_and_scripts(outputs):
    by_lang = Counter(d.language for o in outputs for d in o.docs)
    assert set(by_lang) == {"en", "hi", "ar"} and min(by_lang.values()) > 100
    for o in outputs:
        for d in o.docs:
            assert d.language == o.language
            if d.language == "hi":
                assert DEVANAGARI.search(d.text) and not ARABIC.search(d.text)
            elif d.language == "ar":
                assert ARABIC.search(d.text) and not DEVANAGARI.search(d.text)
            else:
                assert not DEVANAGARI.search(d.text) and not ARABIC.search(d.text)
    types = Counter(d.doc_type for o in outputs for d in o.docs)
    assert set(types) == {
        "clinical_note",
        "discharge_summary",
        "referral_letter",
        "lab_report",
        "family_history_narrative",
    }


def test_templates_and_generated_text_are_nfc(outputs):
    def strings(obj):
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for k, v in obj.items():
                yield from strings(k)
                yield from strings(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                yield from strings(v)

    for table in (
        phrases.T,
        phrases.CONDITIONS,
        phrases.PROCEDURES,
        phrases.RELATIONS,
        phrases.OTHER_PERSONS,
        phrases.MONTHS,
        phrases.SEX,
    ):
        assert all(unicodedata.normalize("NFC", s) == s for s in strings(table))
    for o in outputs:
        for d in o.docs:
            if not d.unicode_variant:  # DC-13 documents deliberately carry variant forms
                assert unicodedata.normalize("NFC", d.text) == d.text
            assert "\r" not in d.text and d.text.endswith("\n")
    variants = [d for o in outputs for d in o.docs if d.unicode_variant]
    assert variants
    assert any(unicodedata.normalize("NFC", d.text) != d.text for d in variants if d.language == "ar")


def test_documents_are_marked_synthetic_in_every_language(outputs):
    markers = {"en": "SYNTHETIC DOCUMENT", "hi": "कृत्रिम दस्तावेज़", "ar": "وثيقة اصطناعية"}
    for o in outputs:
        for d in o.docs:
            assert unicodedata.normalize("NFC", markers[d.language]) in unicodedata.normalize("NFC", d.text)


def _plan_counts(langs: list[str], first_id: int):
    labels: Counter = Counter()
    controls: Counter = Counter()
    residents: Counter = Counter()
    for i, lang in enumerate(langs):
        ctx = make_context(
            first_id + i, lang, "female" if i % 2 else "male", 21 + (i * 13) % 60, records=i % 3 != 0
        )
        plan = plan_resident(ctx, SEED, VERSION, AS_OF)
        assert 1 <= len(plan.scenarios) <= 3
        residents[lang] += 1
        controls[lang] += plan.scenarios == ["DC-00"]
        items = [it for d in plan.docs for sec in d.sections.values() for it in sec if isinstance(it, Item)]
        found = (
            [it.label for it in items if it.label]
            + [a for it in items for a in it.absent]
            + plan.resident_absent
        )
        for lab in found:
            for sid in lab["scenario_ids"]:
                labels[(sid, lang)] += 1
    return labels, controls, residents


def test_difficult_case_quotas_at_evaluation_cohort_scale():
    """Docs/09 §5.4 floors, per language, for a 4,000/3,000/3,000 en/hi/ar population (plan level, no rendering)."""
    labels, controls, residents = _plan_counts(["en"] * 4000 + ["hi"] * 3000 + ["ar"] * 3000, 10_000)
    unmet = {
        f"{sid}/{lang}": labels[(sid, lang)]
        for sid, floor in FLOORS.items()
        for lang in ("en", "hi", "ar")
        if labels[(sid, lang)] < floor * 1.4
    }  # comfortably above every floor, not just at it
    assert not unmet, unmet
    for lang in ("en", "hi", "ar"):
        assert controls[lang] >= 0.15 * residents[lang]


def test_difficult_cases_are_well_represented_even_in_the_smallest_language_slice_of_a_proportional_sample():
    """A random 10,000-resident AI cohort (en 60 / hi 25 / ar 15) has only ~1,500 Arabic residents."""
    labels, controls, residents = _plan_counts(["ar"] * 1500, 300_000)
    assert all(labels[(sid, "ar")] >= 130 for sid in FLOORS if sid != "DC-15"), {
        s: labels[(s, "ar")] for s in FLOORS
    }
    assert labels[("DC-15", "ar")] >= 30 and controls["ar"] >= 0.15 * residents["ar"]


def _scn(outputs, sid):
    return [f for f in _facts(outputs) if sid in f["scenario_ids"]]


def test_scenario_labels_encode_the_documented_expectations(outputs):
    docs = _docs(outputs)
    dc01 = _scn(outputs, "DC-01")
    assert dc01 and all(
        f["category"] == "family_history" and f["assertion"] == "negative" and f["subject"] == "relative"
        for f in dc01
    )
    assert all(
        f["relationship"] in RELATIONSHIPS and {"assertion": "positive"} in f["forbidden_interpretations"]
        for f in dc01
    )

    dc02 = _scn(outputs, "DC-02")
    others = [f for f in dc02 if f["subject"] == "other_person"]
    assert others and all(
        f["relationship"] is None and f["subject_type"] == "other" and f["category"] == "condition"
        for f in others
    )
    swapped = [f for f in dc02 if f["world_truth"]["differs_from_document"]]
    assert swapped and all(
        f["family_edge_id"] is None and f["world_truth"]["true_value"]["relationship"] for f in swapped
    )
    assert any(
        f["forbidden_interpretations"] and f["subject"] == "relative"
        for f in dc02
        if not f["world_truth"]["differs_from_document"]
    )

    dc03 = _scn(outputs, "DC-03")
    assert dc03 and all(
        f["world_truth"]["differs_from_document"] and f["world_truth"]["true_value"] for f in dc03
    )

    dc04 = [
        f for f in _scn(outputs, "DC-04") if f["category"] in ("preventive_care", "procedure") and f["quote"]
    ]
    assert dc04 and all(f["assertion"] == "positive" and f["temporal"] == "historical" for f in dc04)
    assert {f["event_date"]["precision"] if f["event_date"] else None for f in dc04} >= {"day", None}
    assert {f["fact_type"] for f in dc04} & set(phrases.EXCLUSIONS)

    groups = [c for o in outputs for c in o.conflicts]
    assert {c["kind"] for c in groups} >= {"doc_vs_doc", "doc_updates_doc"}
    ids = {f["label_id"]: f for f in _facts(outputs)}
    for c in groups:
        assert c["expected_resolution"] == "preserve_both" and (
            len(c["member_label_ids"]) >= 2 or c["structured_record_refs"]
        )
        assert all(ids[m]["conflict_group_id"] == c["conflict_group_id"] for m in c["member_label_ids"])

    absent = [f for f in _scn(outputs, "DC-06") if not f["expected_present"]]
    assert absent and all(f["quote"] is None and f["expected_validation"] == "rejected" for f in absent)
    assert any(f["document_id"] is None for f in absent) and any(f["document_id"] for f in absent)
    for o in outputs:
        if "DC-06" in o.scenarios:  # nothing in a DC-06 resident's documents plants family history
            assert not [f for f in o.facts if f["category"] == "family_history" and f["expected_present"]]

    dc07 = _scn(outputs, "DC-07")
    assert dc07 and all(f["expected_validation"] == "requires_review" for f in dc07)
    assert {f["subject"] for f in dc07} >= {"unknown", "resident"}

    dups = [f for f in _facts(outputs) if f["duplicate_of"]]
    assert dups and all(f["duplicate_of"] in ids for f in dups)
    plain = [
        f for f in dups if not docs[ids[f["duplicate_of"]]["document_id"]].unicode_variant
    ]  # DC-13 re-spells quotes
    assert plain and all(ids[f["duplicate_of"]]["quote"]["text"] == f["quote"]["text"] for f in plain)

    dc13 = [d for o in outputs for d in o.docs if d.unicode_variant]
    assert all(d.doc_type == "family_history_narrative" for d in dc13)
    assert any("ـ" in d.text or re.search("[٠-٩]", d.text) for d in dc13 if d.language == "ar")
    assert any(re.search("[०-९]", d.text) for d in dc13 if d.language == "hi")
    assert any(len(d.text) > 2500 for d in docs.values())  # DC-12 long documents exist

    fam = [f for f in _facts(outputs) if f["category"] == "family_history" and f["expected_present"]]
    assert fam and all(f["subject"] == "relative" and f["relationship"] in RELATIONSHIPS for f in fam)
    assert all(
        f["subject_person_id"] is None and f["family_edge_id"] is None for f in fam
    )  # no graph: nothing is invented
    positive = [f for f in fam if f["assertion"] == "positive" and f["age_at_onset"] is not None]
    assert positive and all(1 <= f["age_at_onset"] <= 119 for f in positive)


def test_primary_scenarios_follow_the_resident_assignment(outputs):
    for o in outputs:
        scn = set(o.scenarios)
        assert (scn == {"DC-00"}) or ("DC-00" not in scn and 1 <= len(scn) <= 3)
        for f in o.facts:
            assert (
                set(f["scenario_ids"]) <= scn | {"DC-00"} | {"DC-13"} and f["label_origin"] == "programmatic"
            )
    assert Counter(s for o in outputs for s in o.scenarios)["DC-00"] > 60


def test_ground_truth_never_reaches_documents_or_database_columns(outputs):
    from anticipa.datafabric.models import Document

    banned = (
        "DC-0",
        "DC-1",
        "scenario",
        "ground_truth",
        "expected_",
        "gt.fact",
        "label_id",
        "world_truth",
        "forbidden_interpretations",
    )
    for o in outputs:
        for d in o.docs:
            assert not [b for b in banned if b in d.text], d.text[:200]
    columns = {c.name for c in Document.__table__.columns}
    assert not [c for c in columns if re.search(r"scenario|label|quote|truth|expected|ground|offset", c)]
    rows = [d for o in outputs for d in o.docs]
    assert all(
        d.key.endswith(f"/{d.id}.txt") and d.key.startswith(f"{VERSION}/{d.resident_id}/") for d in rows
    )


def test_graph_relatives_are_linked_and_document_only_relatives_are_recorded(contexts):
    import uuid

    from anticipa.datafabric.docgen.context import Relative

    ctx = next(
        c for c in contexts if "DC-10" in assign_scenarios(c, AS_OF)
    )  # a resident whose notes name relatives
    mother = Relative(
        "m-key",
        "mother",
        "female",
        None,
        ctx.birth_date.year - 28,
        resident_id=uuid.uuid4(),
        edge_id=uuid.uuid4(),
        provenance="deterministic_linker",
    )
    ctx.graph_relatives = [mother]
    linked, doc_only = 0, 0
    for seed in range(40):
        out = build_resident(ctx, VERSION, seed, AS_OF)
        for f in out.facts:
            if f["category"] == "family_history" and f["relationship"] == "mother":
                assert f["subject_person_id"] == str(mother.resident_id) and f["family_edge_id"] == str(
                    mother.edge_id
                )
                linked += 1
            elif (
                f["category"] == "family_history"
                and f["related_person_key"]
                and f["related_person_key"].startswith("docrel:")
            ):
                assert f["family_edge_id"] is None and f["subject_person_id"] is None
                doc_only += 1
        assert all(
            r["provenance"] == "document_only" and r["related_resident_id"] is None for r in out.relatives
        )
        assert "mother" not in {r["relationship_type"] for r in out.relatives}  # the graph already has her
    assert linked and doc_only


def test_scenario_assignment_is_independent_of_generation_order(contexts):
    first = [assign_scenarios(c, AS_OF) for c in contexts]
    second = [assign_scenarios(c, AS_OF) for c in reversed(contexts)][::-1]
    assert first == second


@pytest.fixture
def fake_loader(monkeypatch, contexts):
    def loader(dataset_version, *, limit=None, graph=None, as_of):
        chosen = contexts[:limit] if limit else contexts
        for i in range(0, len(chosen), 40):
            yield chosen[i : i + 40]

    monkeypatch.setattr(gen, "contexts_from_synthea", loader)
    monkeypatch.setattr("anticipa.datafabric.synthea.load_manifest", lambda d: {"reference_date": "20261106"})


def _gt_lines(gt_root, name) -> list[bytes]:
    path = gt_root / VERSION / FILES[name]
    return path.read_bytes().splitlines() if path.exists() else []


def test_generate_is_idempotent_resumable_and_verifiable(tmp_path, fake_loader, contexts):
    store, gt = LocalDocumentStore(tmp_path / "docs"), tmp_path / "gt"
    contexts[3].deceased = AS_OF

    def run(n, seed=SEED):
        return gen.generate(VERSION, seed=seed, residents=n, source="synthea", store=store, gt_root=gt)

    first = run(100)
    assert (first.residents_seen, first.residents_generated, first.skipped_deceased) == (100, 99, 1)
    assert first.documents > 250 and first.documents_registered == 0
    before = {n: _gt_lines(gt, n) for n in FILES}
    assert before["facts_dev"] and before["facts_test"] and before["doc_index"] and before["ledger"]
    assert len(before["doc_index"]) == first.documents == len(list((tmp_path / "docs").rglob("*.txt")))

    again = run(100)  # nothing new: no file is rewritten or appended to
    assert (again.residents_generated, again.documents, again.skipped_done) == (0, 0, 99)
    assert {n: _gt_lines(gt, n) for n in FILES} == before

    more = run(160)  # incremental: only the 60 new residents
    assert more.residents_generated == 60 and more.skipped_done == 99 - 0 and more.skipped_deceased == 1
    after = {n: _gt_lines(gt, n) for n in FILES}
    assert all(after[n][: len(before[n])] == before[n] for n in FILES)  # append-only
    label_ids = [orjson.loads(line)["label_id"] for n in ("facts_dev", "facts_test") for line in after[n]]
    assert len(label_ids) == len(set(label_ids))
    assert Counter(orjson.loads(line)["split"] for line in after["facts_dev"]) == {
        "dev": len(after["facts_dev"])
    }

    with pytest.raises(SeedMismatch):
        run(10, seed=SEED + 1)

    assert verify_ground_truth(VERSION, store, root=gt) == {
        "documents": len(after["doc_index"]),
        "labels": len(label_ids),
    }
    manifest = orjson.loads((gt / VERSION / "documents/docgen_manifest.json").read_bytes())
    assert (
        manifest["seed"] == SEED
        and manifest["counts"]["residents"] == 159
        and manifest["generator_version"] == "template_v1"
    )
    for rel, info in manifest["files"].items():
        import hashlib

        assert hashlib.sha256((gt / VERSION / rel).read_bytes()).hexdigest() == info["sha256"]
    assert manifest["quota_report"]["cells"]["DC-00/en"]["achieved"] > 0
    # the label tree and the document store are separate trees; the store holds nothing but documents
    assert not (tmp_path / "docs").samefile(gt) and {
        p.suffix for p in (tmp_path / "docs").rglob("*") if p.is_file()
    } == {".txt"}


def test_verify_detects_a_tampered_document(tmp_path, fake_loader):
    store, gt = LocalDocumentStore(tmp_path / "docs"), tmp_path / "gt"
    gen.generate(VERSION, seed=SEED, residents=20, source="synthea", store=store, gt_root=gt)
    first = orjson.loads(_gt_lines(gt, "doc_index")[0])
    key = f"{VERSION}/{first['resident_id']}/{first['document_id']}.txt"
    store.write_text(key, "X" + store.read_text(key))  # shifts every offset in that document
    result = verify_ground_truth(VERSION, store, root=gt)
    assert result["hash_mismatches"] == 1 and result.get("quote_mismatches", 0) >= 0
    assert result["hash_mismatches"]


def test_job_handler_is_registered():
    import anticipa.datafabric.docgen.handlers  # noqa: F401
    from anticipa.jobs.registry import get_handler

    assert get_handler("docgen.generate_batch") is not None


def test_cli_passes_its_options_to_generate(monkeypatch, capsys):
    from anticipa.datafabric.docgen import cli

    seen = {}

    def fake(version, **kw):
        seen.update(version=version, **kw)
        return gen.RunReport(version, kw["seed"], "2026-11-06")

    monkeypatch.setattr(gen, "generate", fake)
    argv = [
        "generate",
        "--dataset-version",
        VERSION,
        "--residents",
        "250",
        "--seed",
        "9",
        "--source",
        "synthea",
        "--no-enrich",
    ]
    assert cli.main(argv) == 0
    assert (seen["residents"], seen["seed"], seen["source"], seen["enrich"], seen["register"]) == (
        250,
        9,
        "synthea",
        False,
        None,
    )
    assert '"seed": 9' in capsys.readouterr().out
    cli.main(["generate", "--dataset-version", VERSION, "--residents", "all", "--seed", "1"])
    assert seen["residents"] is None and seen["enrich"] is True


def test_output_does_not_depend_on_the_row_order_of_equal_dated_records():
    """Real records share dates constantly and the database returns ties in any order: the documents must not care."""
    from anticipa.datafabric.docgen.context import ResidentContext, add_record, finalize
    from tests.docgen.conftest import SNOMED

    def build(order):
        ctx = ResidentContext(
            id=make_context(1).id,
            source_patient_id="p",
            birth_date=AS_OF.replace(year=1970),
            sex="female",
            language="hi",
            district="D01",
        )
        for i in order:
            add_record(
                ctx,
                "condition",
                SNOMED,
                f"c{i}",
                f"Condition number {i}",
                AS_OF.replace(year=2020),
                None,
                "active",
                {},
                f"s{i}",
            )
            add_record(
                ctx,
                "medication",
                None,
                f"m{i}",
                f"Medicine {i} 10 MG Oral Tablet",
                AS_OF.replace(year=2021),
                None,
                "active",
                {},
                f"m{i}",
            )
            add_record(
                ctx,
                "observation",
                None,
                f"o{i}",
                "HbA1c",
                AS_OF.replace(year=2022),
                None,
                "final",
                {"tag": "hba1c", "value": 5.0 + i / 10, "unit": "%"},
                f"o{i}",
            )
            add_record(
                ctx,
                "procedure",
                SNOMED,
                f"p{i}",
                "Colonoscopy",
                AS_OF.replace(year=2019),
                None,
                "completed",
                {"tag": "colonoscopy"},
                f"p{i}",
            )
        return finalize(ctx, AS_OF)

    forward, backward = build(range(6)), build(reversed(range(6)))
    assert [c.display for c in forward.conditions] == [c.display for c in backward.conditions]
    for seed in range(5):
        a, b = build_resident(forward, VERSION, seed, AS_OF), build_resident(backward, VERSION, seed, AS_OF)
        assert [d.id for d in a.docs] == [d.id for d in b.docs]


def _family_labels(outputs):
    return [
        f for o in outputs for f in o.facts if f["category"] == "family_history" and f["expected_present"]
    ]


def test_unlinked_residents_get_no_family_dependent_facts(contexts):
    from dataclasses import replace

    unlinked = [replace(c, linked=False) for c in contexts]
    outs = [build_resident(c, VERSION, SEED, AS_OF) for c in unlinked]
    assert all(o.linked is False for o in outs)
    assert not _family_labels(outs)
    assert not [
        f
        for o in outs
        for f in o.facts
        if f["subject"] in ("relative", "other_person") and f["expected_present"]
    ]
    assert not [d for o in outs for d in o.docs if d.doc_type == "family_history_narrative"]
    allowed = {"DC-00", "DC-04", "DC-06", "DC-09", "DC-15"}
    assert all(set(o.scenarios) <= allowed for o in outs)
    assert any("DC-04" in o.scenarios for o in outs) and any("DC-15" in o.scenarios for o in outs)
    assert not [r for o in outs for r in o.relatives]  # no document-only relatives are invented either
    # the same residents, when linked, do carry family facts: the switch is the only difference
    assert _family_labels([build_resident(c, VERSION, SEED, AS_OF) for c in contexts])


def test_linked_resident_ids_follow_the_family_linker_blocks(monkeypatch):
    import uuid

    from anticipa.datafabric import family
    from anticipa.datafabric.docgen.context import linked_resident_ids

    ids = {i: [uuid.uuid5(uuid.NAMESPACE_DNS, f"b{i}-{n}") for n in range(3)] for i in range(6)}
    monkeypatch.setattr(
        family, "block_resident_ids", lambda version, data_dir, batches: [x for b in batches for x in ids[b]]
    )
    manifest = {"family_linker": {"blocks": {"0": {"batches": [0, 2]}, "2": {"batches": [4, 6]}}}}
    assert linked_resident_ids(VERSION, manifest) == {x for b in (0, 1, 4, 5) for x in ids[b]}
    assert linked_resident_ids(VERSION, {}) == set() and linked_resident_ids(VERSION, None) == set()


def test_generate_family_modes_and_only_linked(tmp_path, fake_loader):
    def run(name, **kw):
        return gen.generate(
            VERSION,
            seed=SEED,
            residents=60,
            source="synthea",
            store=LocalDocumentStore(tmp_path / name / "d"),
            gt_root=tmp_path / name / "gt",
            **kw,
        )

    def labels(name):
        return [
            orjson.loads(x) for n in ("facts_dev", "facts_test") for x in _gt_lines(tmp_path / name / "gt", n)
        ]

    run("x")  # the file source defaults to family="all"
    assert any(f["category"] == "family_history" for f in labels("x"))
    none = run("none", family="none")
    assert none.residents_generated == 60 and not [
        f for f in labels("none") if f["category"] == "family_history" and f["expected_present"]
    ]
    only = run(
        "only", family="linked", only_linked=True
    )  # the file source records no linker blocks: nobody is linked
    assert (only.residents_generated, only.skipped_unlinked, only.documents) == (0, 60, 0)
    manifest = orjson.loads(
        (tmp_path / "none" / "gt" / VERSION / "documents/docgen_manifest.json").read_bytes()
    )
    assert manifest["family"]["mode"] == "none" and manifest["counts"]["residents_unlinked"] == 60
    ledger = [orjson.loads(x) for x in _gt_lines(tmp_path / "none" / "gt", "ledger")]
    assert ledger and all(item["linked"] is False for item in ledger)


def test_manifest_counts_are_recovered_from_the_files_after_a_crash(tmp_path, fake_loader):
    store, gt = LocalDocumentStore(tmp_path / "docs"), tmp_path / "gt"
    gen.generate(VERSION, seed=SEED, residents=80, source="synthea", store=store, gt_root=gt, family="all")
    path = gt / VERSION / "documents/docgen_manifest.json"
    original = orjson.loads(path.read_bytes())["counts"]
    path.unlink()  # a run killed before it wrote its manifest leaves labels and ledger but no counts
    again = gen.generate(
        VERSION, seed=SEED, residents=80, source="synthea", store=store, gt_root=gt, family="all"
    )
    assert again.residents_generated == 0
    recovered = orjson.loads(path.read_bytes())["counts"]
    assert recovered == original
