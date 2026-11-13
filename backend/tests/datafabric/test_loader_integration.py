"""Loader + family linker + job handler against the test database (`anticipa_test`, rolled back per test)."""
# ruff: noqa: E501

from __future__ import annotations

from hashlib import sha256

import orjson
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from anticipa.core.ids import resident_uuid
from anticipa.datafabric import handlers
from anticipa.datafabric.family import link_block, link_ready_blocks
from anticipa.datafabric.loader import ingest_batch, ingest_dataset
from anticipa.datafabric.models import ClinicalRecord, Dataset, FamilyEdge, Resident
from anticipa.datafabric.normalize import ALL_TYPES
from anticipa.jobs import service
from anticipa.jobs.worker import WorkerConfig, run_one

V = "syn-20261108-s7"


def _run_until_done(sessions, cfg, session, job) -> None:
    """Run the worker until `job` leaves `queued`. The shared test DB may hold other modules' committed queued jobs;
    the worker claims jobs in queue order, so those are processed (and failed, rolled back with the test) first."""
    for _ in range(100):
        if not run_one(sessions, "w1", cfg):
            break
        session.refresh(job)
        if job.status != "queued":
            return
    session.refresh(job)


def _count(session, model, ds, via=None) -> int:
    """Rows of `model` belonging to the dataset (the shared test DB may hold rows from other tests' committed data)."""
    if via is None:
        return session.execute(
            select(func.count()).select_from(model).where(model.dataset_id == ds.id)
        ).scalar_one()
    return session.execute(
        select(func.count())
        .select_from(model)
        .join(Resident, Resident.id == via)
        .where(Resident.dataset_id == ds.id)
    ).scalar_one()


def test_ingest_loads_rows_and_is_idempotent(db_session, fhir, tmp_path):
    fhir.generate(tmp_path, residents=40, batch_size=20)
    r0 = ingest_batch(db_session, dataset_version=V, batch_index=0, data_dir=tmp_path)
    assert (r0["residents_inserted"], r0["clinical_inserted"]) == (20, 20 * 8)
    assert (
        r0["emitted"]["Observation"] == 40
        and r0["filtered_out"]["Observation"] == 20
        and not r0["quarantined"]
    )

    ds = db_session.execute(select(Dataset).where(Dataset.dataset_version == V)).scalar_one()
    assert ds.status == "loading" and ds.generator == "synthea" and ds.base_seed == 7
    assert ds.manifest["generation"]["batches_planned"] == 2 and "0" in ds.manifest["ingest"]["batches"]

    res = db_session.execute(select(Resident).where(Resident.dataset_id == ds.id)).scalars().all()
    assert len(res) == 20 and {r.synthetic_status for r in res} == {"synthetic"}
    assert {r.source_system for r in res} == {f"synthea:{V}"}
    assert all(r.id == resident_uuid(V, r.source_patient_id) for r in res)  # contract 17 §2 UUIDv5
    assert all(r.district_code and r.preferred_language in {"en", "hi", "ar"} for r in res)
    rec = db_session.execute(
        select(ClinicalRecord).where(ClinicalRecord.category == "condition").limit(1)
    ).scalar_one()
    assert (
        rec.source_reference.startswith(f"synthea://{V}/batch-0/Condition/")
        and rec.synthetic_status == "synthetic"
    )
    assert (
        rec.code_system == "http://snomed.info/sct" and rec.effective_start.utcoffset().total_seconds() == 0
    )

    again = ingest_batch(db_session, dataset_version=V, batch_index=0, data_dir=tmp_path)
    assert again["skipped"] is True  # same generation sha already recorded
    forced = ingest_batch(db_session, dataset_version=V, batch_index=0, data_dir=tmp_path, force=True)
    assert (forced["residents_inserted"], forced["clinical_inserted"]) == (0, 0)  # ON CONFLICT DO NOTHING
    assert _count(db_session, Resident, ds) == 20

    ingest_dataset(db_session, V, data_dir=tmp_path)  # loads batch 1 and skips batch 0
    db_session.refresh(ds)
    assert ds.status == "loaded" and set(ds.manifest["ingest"]["batches"]) == {"0", "1"}
    assert _count(db_session, Resident, ds) == 40


def test_ingest_quarantines_bad_lines_but_loads_the_rest(db_session, fhir, tmp_path):
    def runner(spec):
        res = fhir.resources(spec.seed, spec.residents)
        fhir.write_batch(spec.batch_dir / "fhir", res)
        with open(spec.batch_dir / "fhir" / "Observation.ndjson", "ab") as fh:
            fh.write(b"{broken\n")
        return 1.0

    fhir.generate(tmp_path, residents=10, batch_size=10, runner=runner)
    r = ingest_batch(db_session, dataset_version=V, batch_index=0, data_dir=tmp_path)
    assert r["quarantined"] == {"invalid_json": 1} and r["clinical_inserted"] == 10 * 8
    q = (tmp_path / V / "batch-0" / "quarantine.ndjson").read_text().splitlines()
    assert orjson.loads(q[0])["reason"] == "invalid_json"


def test_ingest_rejects_an_incomplete_batch(db_session, fhir, tmp_path):
    fhir.generate(tmp_path, residents=10, batch_size=10)
    with pytest.raises(ValueError, match="not a completed batch"):
        ingest_batch(db_session, dataset_version=V, batch_index=5, data_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        ingest_batch(db_session, dataset_version="syn-20261108-s999", batch_index=0, data_dir=tmp_path)


def _edges(session, ds):
    return (
        session.execute(
            select(FamilyEdge)
            .join(Resident, Resident.id == FamilyEdge.resident_id)
            .where(Resident.dataset_id == ds.id)
        )
        .scalars()
        .all()
    )


def test_family_linker_writes_db_edges_and_matching_ground_truth(db_session, fhir, tmp_path):
    fhir.generate(tmp_path, residents=600, batch_size=300, clinical=False)
    ingest_dataset(db_session, V, data_dir=tmp_path)
    gt_root = tmp_path / "gt"
    [r] = link_ready_blocks(db_session, V, block_batches=2, data_dir=tmp_path, gt_root=gt_root)
    assert (
        r.block == 0 and r.units > 20 and r.edges > 100 and 0.5 < r.residents_in_families / r.residents < 0.8
    )

    ds = db_session.execute(select(Dataset).where(Dataset.dataset_version == V)).scalar_one()
    edges = _edges(db_session, ds)
    assert len(edges) == r.edges
    assert {e.source_type for e in edges} == {"deterministic_linker"}
    assert {e.synthetic_status for e in edges} == {"synthetic"}
    assert all(
        e.resident_id != e.related_resident_id and e.relationship_direction == "related_to_resident"
        for e in edges
    )
    by_pair = {(e.resident_id, e.related_resident_id): e.relationship_type for e in edges}
    assert all((b, a) in by_pair for a, b in by_pair)  # every edge has its reciprocal row

    gt_file = gt_root / V / "families" / "family_graph.jsonl"
    gt = [orjson.loads(line) for line in gt_file.read_text().splitlines()]
    assert len(gt) == len(edges) and {g["schema_version"] for g in gt} == {"gt.family_edge.v1"}
    assert {g["edge_id"] for g in gt} == {str(e.id) for e in edges}  # same ids in the DB and the ground truth
    assert all(g["provenance"] == "deterministic_linker" and g["decoy"] is False for g in gt)
    assert any(g["household_id"] for g in gt)
    meta = orjson.loads((gt_file.parent / "family_graph.meta.json").read_bytes())
    assert (
        meta["edges"] == r.edges
        and meta["ground_truth_edges"] == r.edges
        and meta["blocks"]["0"]["units"] == r.units
    )
    db_session.refresh(ds)
    assert ds.manifest["family_linker"]["edges"] == r.edges
    # ground truth never reaches the database free text: the dataset manifest holds counts and a file name only
    assert "relationship_type" not in orjson.dumps(ds.manifest["generation"]).decode()

    assert (
        link_ready_blocks(db_session, V, block_batches=2, data_dir=tmp_path, gt_root=gt_root) == []
    )  # already linked
    again = link_block(
        db_session, V, 0, block_batches=2, data_dir=tmp_path, gt_root=gt_root
    )  # idempotent + deterministic
    assert again.ground_truth_sha256 == r.ground_truth_sha256
    assert len(_edges(db_session, ds)) == r.edges


def test_growing_a_dataset_never_changes_linked_families(db_session, fhir, tmp_path):
    gt_root = tmp_path / "gt"
    fhir.generate(
        tmp_path, residents=300, batch_size=150, clinical=False
    )  # 2 batches = block 0 (block_batches=2)
    ingest_dataset(db_session, V, data_dir=tmp_path)
    [b0] = link_ready_blocks(db_session, V, block_batches=2, data_dir=tmp_path, gt_root=gt_root)
    ds = db_session.execute(select(Dataset).where(Dataset.dataset_version == V)).scalar_one()
    before = {
        (e.id, e.resident_id, e.related_resident_id, e.relationship_type) for e in _edges(db_session, ds)
    }
    shard_sha = b0.ground_truth_sha256

    fhir.generate(
        tmp_path, residents=750, batch_size=150, clinical=False
    )  # extend: 5 batches = 2 full blocks + 1
    ingest_dataset(db_session, V, data_dir=tmp_path)
    ls = link_ready_blocks(db_session, V, block_batches=2, data_dir=tmp_path, gt_root=gt_root)
    assert [r.block for r in ls] == [1]  # block 2 is partial (1 of 2 batches): not linked
    db_session.refresh(ds)
    assert set(ds.manifest["family_linker"]["blocks"]) == {"0", "1"}
    edges = _edges(db_session, ds)
    assert before <= {
        (e.id, e.resident_id, e.related_resident_id, e.relationship_type) for e in edges
    }  # block 0 intact
    assert len(edges) == b0.edges + ls[0].edges
    block1_residents = {e.resident_id for e in edges} - {x[1] for x in before}
    assert block1_residents and not block1_residents & {x[1] for x in before}  # no edge crosses blocks
    fam = gt_root / V / "families"
    assert (
        sha256((fam / "family_graph.block-0000.jsonl").read_bytes()).hexdigest() == shard_sha
    )  # shard untouched
    merged = (fam / "family_graph.jsonl").read_bytes()
    assert (
        merged
        == (fam / "family_graph.block-0000.jsonl").read_bytes()
        + (fam / "family_graph.block-0001.jsonl").read_bytes()
    )
    assert (
        link_ready_blocks(
            db_session, V, block_batches=2, data_dir=tmp_path, gt_root=gt_root, allow_partial=True
        )[0].block
        == 2
    )  # the partial block links only when explicitly allowed


def test_link_block_refuses_unloaded_or_partial_blocks(db_session, fhir, tmp_path):
    fhir.generate(tmp_path, residents=300, batch_size=100, clinical=False)  # 3 batches
    ingest_dataset(db_session, V, batches=[0, 1], data_dir=tmp_path)
    with pytest.raises(ValueError, match="not fully loaded"):
        link_block(
            db_session, V, 1, block_batches=2, data_dir=tmp_path, gt_root=tmp_path / "gt", allow_partial=True
        )
    with pytest.raises(ValueError, match="partial"):
        link_block(db_session, V, 1, block_batches=2, data_dir=tmp_path, gt_root=tmp_path / "gt")
    with pytest.raises(ValueError, match="outside"):
        link_block(db_session, V, 9, block_batches=2, data_dir=tmp_path, gt_root=tmp_path / "gt")
    assert [
        r.block
        for r in link_ready_blocks(db_session, V, block_batches=2, data_dir=tmp_path, gt_root=tmp_path / "gt")
    ] == [0]


def test_family_linker_requires_a_loaded_dataset(db_session, tmp_path):
    with pytest.raises(ValueError, match="unknown dataset"):
        link_ready_blocks(db_session, "syn-20261108-s404", data_dir=tmp_path, gt_root=tmp_path)


def test_ingest_job_handler_runs_through_the_worker(db_session, fhir, tmp_path, monkeypatch):
    fhir.generate(tmp_path, residents=20, batch_size=10)
    monkeypatch.setattr(handlers, "SYNTHEA_DIR", tmp_path)
    sessions = sessionmaker(
        bind=db_session.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    cfg = WorkerConfig(
        poll_interval=0.0, heartbeat_interval=3600.0, stale_after=60.0, backoff_base=0.0, backoff_cap=0.0
    )

    job = service.submit_job(db_session, "datafabric.ingest_batch", {"dataset_version": V}).job
    db_session.commit()
    _run_until_done(sessions, cfg, db_session, job)
    db_session.refresh(job)
    assert job.status == "completed" and (job.total_items, job.succeeded_items, job.failed_items) == (2, 2, 0)
    ds = db_session.execute(select(Dataset).where(Dataset.dataset_version == V)).scalar_one()
    assert _count(db_session, Resident, ds) == 20 and ds.status == "loaded"

    bad = service.submit_job(
        db_session, "datafabric.ingest_batch", {"dataset_version": "../etc"}, max_retries=0
    ).job
    db_session.commit()
    _run_until_done(sessions, cfg, db_session, bad)
    db_session.refresh(bad)
    assert bad.status == "failed" and "dataset_version" in (bad.error_summary or "")


def test_document_reference_stubs_are_opt_in(db_session, fhir, tmp_path):
    fhir.generate(tmp_path, residents=10, batch_size=10)
    default = ingest_batch(db_session, dataset_version=V, batch_index=0, data_dir=tmp_path)
    assert default["clinical_inserted"] == 10 * 8 and "DocumentReference" not in default["emitted"]
    extra = ingest_batch(
        db_session, dataset_version=V, batch_index=0, data_dir=tmp_path, force=True, types=ALL_TYPES
    )
    assert (
        extra["clinical_inserted"] == 10 and extra["emitted"]["DocumentReference"] == 10
    )  # only the new stubs insert
