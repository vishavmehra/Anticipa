# ruff: noqa: E501
"""Database path (integration, DB `anticipa_test` via the shared `db_session` fixture): idempotent registration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import orjson
import pytest
from sqlalchemy import func, select

from anticipa.datafabric.docgen import generate as gen
from anticipa.datafabric.docgen.groundtruth import FILES, verify_ground_truth
from anticipa.datafabric.docstore import LocalDocumentStore, content_hash
from anticipa.datafabric.models import ClinicalRecord, Dataset, Document, Resident
from tests.docgen.conftest import AS_OF, make_context

VERSION = "syn-20261106-s4242"  # unique to this test; rolled back with the test transaction


def generate(*args, **kw):
    """The test dataset has no family-linker blocks: ask for family facts explicitly unless a test says otherwise."""
    kw.setdefault("family", "all")
    return gen.generate(*args, **kw)


class _Keep:
    """Hands out the shared test session without closing it (generate() uses `with session_factory() as s`)."""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False


@pytest.fixture
def loaded(db_session):
    ds = Dataset(
        dataset_version=VERSION, generator="synthea", status="loaded", manifest={"reference_date": "20261106"}
    )
    db_session.add(ds)
    db_session.flush()
    contexts = [
        make_context(90_000 + i, ("en", "hi", "ar")[i % 3], "female" if i % 2 else "male", 30 + (i * 3) % 50)
        for i in range(90)
    ]
    for c in contexts:
        db_session.add(
            Resident(
                id=c.id,
                dataset_id=ds.id,
                source_system=f"synthea:{VERSION}",
                source_patient_id=c.source_patient_id,
                birth_date=c.birth_date,
                administrative_sex=c.sex,
                preferred_language=c.language,
                district_code="D01",
                synthetic_status="synthetic",
            )
        )
    db_session.flush()
    for c in contexts:
        for n, rec in enumerate(c.conditions + c.medications + c.procedures + list(c.observations.values())):
            db_session.add(
                ClinicalRecord(
                    resident_id=c.id,
                    resource_type=rec.category.title(),
                    source_resource_id=f"{rec.category}-{n}",
                    category=rec.category,
                    code_system=rec.code_system,
                    code=rec.code,
                    display=rec.display,
                    effective_start=datetime(rec.start.year, rec.start.month, rec.start.day, tzinfo=UTC),
                    status=rec.status,
                    value_json=rec.value or None,
                    source_reference=f"test://{c.id}/{n}",
                    synthetic_status="synthetic",
                )
            )
        db_session.add(
            ClinicalRecord(
                resident_id=c.id,
                resource_type="Encounter",
                source_resource_id="enc",
                category="encounter",
                effective_start=datetime.combine(c.last_encounter, datetime.min.time(), tzinfo=UTC),
                source_reference=f"test://{c.id}/enc",
                synthetic_status="synthetic",
            )
        )
    db_session.flush()
    return contexts


def _counts(session):
    """(documents, enrichment rows) belonging to this test's dataset (the shared test DB may hold other rows)."""
    docs = session.execute(
        select(func.count()).select_from(Document).where(Document.storage_key.like(f"{VERSION}/%"))
    ).scalar_one()
    enriched = session.execute(
        select(func.count())
        .select_from(ClinicalRecord)
        .where(
            ClinicalRecord.source_system == "anticipa_enrichment",
            ClinicalRecord.source_reference.like(f"enrichment://{VERSION}/%"),
        )
    ).scalar_one()
    return docs, enriched


def test_registration_is_idempotent_and_documents_are_readable(db_session, loaded, tmp_path):
    store, gt = LocalDocumentStore(tmp_path / "docs"), tmp_path / "gt"

    def run(**kw):
        return generate(
            VERSION,
            seed=5,
            source="db",
            store=store,
            gt_root=gt,
            session_factory=lambda: _Keep(db_session),
            **kw,
        )

    first = run(chunk_size=40)
    assert first.residents_generated == 90 and first.documents > 200
    docs, enriched = _counts(db_session)
    assert (
        docs == first.documents_registered == first.documents
        and enriched == first.enrichment_records_registered > 20
    )

    rows = (
        db_session.execute(select(Document).where(Document.storage_key.like(f"{VERSION}/%"))).scalars().all()
    )
    for d in rows[:60]:  # what the AI workstream does: read through the store with the registered key
        text = store.read_text(d.storage_key)
        assert content_hash(text) == d.content_hash
        assert (d.synthetic_status, d.generation_method, d.processing_status, d.source_system) == (
            "synthetic",
            "template_v1",
            "registered",
            "anticipa",
        )
        assert d.storage_key.startswith(f"{VERSION}/{d.resident_id}/") and d.created_on.tzinfo is not None
    assert {d.language for d in rows} == {"en", "hi", "ar"}

    enriched_rows = (
        db_session.execute(
            select(ClinicalRecord).where(
                ClinicalRecord.source_system == "anticipa_enrichment",
                ClinicalRecord.source_reference.like(f"enrichment://{VERSION}/%"),
            )
        )
        .scalars()
        .all()
    )
    assert all(
        r.source_reference.startswith("enrichment://") and r.synthetic_status == "synthetic"
        for r in enriched_rows
    )

    again = run(chunk_size=40)  # nothing new: labels are done, registration is a no-op, nothing is duplicated
    assert (
        again.residents_generated,
        again.documents,
        again.documents_registered,
        again.enrichment_records_registered,
    ) == (0, 0, 0, 0)
    assert _counts(db_session) == (docs, enriched)
    assert verify_ground_truth(VERSION, store, root=gt)["documents"] == docs

    # enrichment rows are never read back as source records: the plan (and so every document) stays identical
    third = run(chunk_size=17)
    assert third.documents_registered == 0 and _counts(db_session) == (docs, enriched)


def test_documents_can_be_registered_after_a_file_only_run(db_session, loaded, tmp_path):
    """A first run without a database (labels written) must still register documents on the next run with one."""
    store, gt = LocalDocumentStore(tmp_path / "docs"), tmp_path / "gt"
    file_only = generate(
        VERSION,
        seed=5,
        source="db",
        store=store,
        gt_root=gt,
        register=False,
        session_factory=lambda: _Keep(db_session),
    )
    assert file_only.documents > 200 and _counts(db_session) == (0, 0)
    registered = generate(
        VERSION, seed=5, source="db", store=store, gt_root=gt, session_factory=lambda: _Keep(db_session)
    )
    assert registered.residents_generated == 0 and registered.documents_registered == file_only.documents
    assert _counts(db_session)[0] == file_only.documents


def test_deceased_residents_are_skipped_and_the_limit_is_honoured(db_session, loaded, tmp_path):
    first_by_id = min(loaded, key=lambda c: c.id)  # residents are processed in id order
    db_session.execute(
        Resident.__table__.update()
        .where(Resident.id == first_by_id.id)
        .values(deceased_date=AS_OF - timedelta(days=5))
    )
    db_session.flush()
    report = generate(
        VERSION,
        seed=5,
        residents=10,
        source="db",
        store=LocalDocumentStore(tmp_path / "d"),
        gt_root=tmp_path / "g",
        session_factory=lambda: _Keep(db_session),
    )
    assert (report.residents_seen, report.skipped_deceased, report.residents_generated) == (10, 1, 9)
    count = select(func.count()).select_from(Document).where(Document.resident_id == first_by_id.id)
    assert db_session.execute(count).scalar_one() == 0


def test_reset_is_a_dry_run_by_default_and_only_removes_docgen_output(db_session, loaded, tmp_path):
    store_root, gt = tmp_path / "docs", tmp_path / "gt"
    generate(
        VERSION,
        seed=5,
        residents=20,
        source="db",
        store=LocalDocumentStore(store_root),
        gt_root=gt,
        session_factory=lambda: _Keep(db_session),
    )
    graph = gt / VERSION / "families" / "family_graph.jsonl"  # belongs to the family linker, never touched
    graph.parent.mkdir(parents=True, exist_ok=True)
    graph.write_text("{}\n", encoding="utf-8")
    before = _counts(db_session)
    assert before[0] > 50

    dry = gen.reset_generated(VERSION, session=db_session, store_root=store_root, gt_root=gt)
    assert (
        dry["applied"] is False and dry["documents"] == before[0] and dry["enrichment_records"] == before[1]
    )
    assert _counts(db_session) == before and (store_root / VERSION).exists()

    done = gen.reset_generated(VERSION, session=db_session, store_root=store_root, gt_root=gt, apply=True)
    assert done["applied"] is True and _counts(db_session) == (0, 0)
    assert not (store_root / VERSION).exists() and not (gt / VERSION / "facts").exists()
    assert not (gt / VERSION / "documents" / "docgen_ledger.jsonl").exists() and graph.exists()
    again = generate(  # a reset dataset can be generated again from scratch
        VERSION,
        seed=6,
        residents=20,
        source="db",
        store=LocalDocumentStore(store_root),
        gt_root=gt,
        session_factory=lambda: _Keep(db_session),
    )
    assert again.residents_generated == 20 and _counts(db_session)[0] == again.documents_registered


def test_a_changed_context_never_registers_documents_without_labels(db_session, loaded, tmp_path):
    store, gt = LocalDocumentStore(tmp_path / "docs"), tmp_path / "gt"
    factory = lambda: _Keep(db_session)  # noqa: E731
    first = generate(
        VERSION, seed=5, residents=15, source="db", store=store, gt_root=gt, session_factory=factory
    )
    before = _counts(db_session)
    target = min(loaded, key=lambda c: c.id)
    db_session.add(  # a record that was loaded after the first run: it changes this resident's documents
        ClinicalRecord(
            resident_id=target.id,
            resource_type="Condition",
            source_resource_id="late",
            category="condition",
            code_system="http://snomed.info/sct",
            code="44054006",
            display="Type 2 diabetes mellitus (disorder)",
            effective_start=datetime(2026, 10, 30, tzinfo=UTC),
            status="active",
            source_reference="test://late",
            synthetic_status="synthetic",
        )
    )
    db_session.flush()
    again = generate(
        VERSION, seed=5, residents=15, source="db", store=store, gt_root=gt, session_factory=factory
    )
    assert again.residents_generated == 0 and again.drifted_residents == 1
    assert _counts(db_session) == before and first.documents == before[0]
    assert verify_ground_truth(VERSION, store, root=gt)["documents"] == before[0]


def test_only_residents_of_linked_blocks_receive_family_facts(db_session, loaded, tmp_path, monkeypatch):
    ordered = sorted(loaded, key=lambda c: c.id)
    linked = {c.id for c in ordered[::2]}
    monkeypatch.setattr(gen, "linked_resident_ids", lambda version, manifest: linked)
    store, gt = LocalDocumentStore(tmp_path / "docs"), tmp_path / "gt"
    report = gen.generate(
        VERSION, seed=5, source="db", store=store, gt_root=gt, session_factory=lambda: _Keep(db_session)
    )
    assert report.residents_generated == len(loaded)
    labels = [
        orjson.loads(x)
        for n in ("facts_dev", "facts_test")
        for x in (gt / VERSION / FILES[n]).read_bytes().splitlines()
    ]
    with_family = {
        f["resident_id"] for f in labels if f["category"] == "family_history" and f["expected_present"]
    }
    assert with_family and with_family <= {str(i) for i in linked}
    ledger = {
        orjson.loads(x)["resident_id"]: orjson.loads(x)["linked"]
        for x in (gt / VERSION / FILES["ledger"]).read_bytes().splitlines()
    }
    assert {r for r, flag in ledger.items() if flag} == {str(i) for i in linked}

    later = gen.generate(
        VERSION,
        seed=5,
        source="db",
        store=store,
        gt_root=gt,
        only_linked=True,
        session_factory=lambda: _Keep(db_session),
    )
    assert later.skipped_unlinked == len(loaded) - len(linked) and later.residents_generated == 0


def test_refresh_linked_regenerates_only_residents_generated_before_their_block_was_linked(
    db_session, loaded, tmp_path, monkeypatch
):
    ordered = sorted(loaded, key=lambda c: c.id)
    early, late = {c.id for c in ordered[:45]}, {c.id for c in ordered[45:]}
    state = {"linked": set(early)}
    monkeypatch.setattr(gen, "linked_resident_ids", lambda version, manifest: state["linked"])
    store, gt = LocalDocumentStore(tmp_path / "docs"), tmp_path / "gt"

    def run(**kw):
        return gen.generate(
            VERSION,
            seed=5,
            source="db",
            store=store,
            gt_root=gt,
            session_factory=lambda: _Keep(db_session),
            **kw,
        )

    def family_residents():
        rows = [
            orjson.loads(x)
            for n in ("facts_dev", "facts_test")
            for x in (gt / VERSION / FILES[n]).read_bytes().splitlines()
        ]
        return {f["resident_id"] for f in rows if f["category"] == "family_history" and f["expected_present"]}

    run()
    assert not family_residents() & {str(i) for i in late}  # generated while unlinked: no family facts
    kept = {str(c.id): {d for d in (store.root / VERSION / str(c.id)).glob("*.txt")} for c in ordered[:45]}

    state["linked"] = early | late  # the family linker finishes the second block
    stale = run()
    assert (
        stale.residents_generated == 0 and stale.drifted_residents > 0
    )  # without a refresh they stay as they were
    refreshed = run(refresh_linked=True)
    assert refreshed.refreshed_after_linking == 45 and refreshed.residents_generated == 45
    assert family_residents() & {str(i) for i in late}
    assert {
        str(c.id): {d for d in (store.root / VERSION / str(c.id)).glob("*.txt")} for c in ordered[:45]
    } == kept

    index = [orjson.loads(x) for x in (gt / VERSION / FILES["doc_index"]).read_bytes().splitlines()]
    assert len(index) == _counts(db_session)[0] and len({d["document_id"] for d in index}) == len(index)
    ledger = [orjson.loads(x) for x in (gt / VERSION / FILES["ledger"]).read_bytes().splitlines()]
    assert len(ledger) == 90 and all(item["linked"] for item in ledger)
    ids = [
        orjson.loads(x)["label_id"]
        for n in ("facts_dev", "facts_test")
        for x in (gt / VERSION / FILES[n]).read_bytes().splitlines()
    ]
    assert len(ids) == len(set(ids))
    assert verify_ground_truth(VERSION, store, root=gt)["documents"] == len(index)
    assert run().residents_generated == 0 and run(refresh_linked=True).refreshed_after_linking == 0
    manifest = orjson.loads((gt / VERSION / "documents/docgen_manifest.json").read_bytes())
    assert manifest["counts"]["residents"] == 90 and manifest["counts"].get("residents_unlinked", 0) == 0
