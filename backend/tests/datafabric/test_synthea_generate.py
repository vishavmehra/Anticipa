"""Batch generator: manifest, resume, failure handling, gzip, seeds (fake runner, no JVM)."""

from __future__ import annotations

import gzip

import orjson
import pytest

from anticipa.datafabric import synthea


def _ids(batch_dir):
    with gzip.open(batch_dir / "Patient.ndjson.gz", "rb") as fh:
        return [orjson.loads(line)["id"] for line in fh]


def test_generates_batches_with_manifest_and_gzip(tmp_path, fhir):
    m = fhir.generate(tmp_path, residents=25, batch_size=10)  # 10 + 10 + 5
    assert m["status"] == "generated" and m["totals"]["residents_completed"] == 25
    assert [m["batches"][str(i)]["residents"] for i in range(3)] == [10, 10, 5]
    d = tmp_path / "syn-20261108-s7"
    for i in range(3):
        b = d / f"batch-{i}"
        assert (b / "Patient.ndjson.gz").exists() and not (
            b / "fhir"
        ).exists()  # raw output removed after gzip
        assert m["batches"][str(i)]["seed"] == 7 + i  # seed = base_seed + batch_index (contract 17 §7)
    all_ids = [pid for i in range(3) for pid in _ids(d / f"batch-{i}")]
    assert len(all_ids) == len(set(all_ids)) == 25  # no duplicate resident identities across batches
    assert m["totals"]["resource_counts"]["Patient"] == 25 and m["reference_date"] == "20261108"
    on_disk = orjson.loads((d / "manifest.json").read_bytes())
    assert on_disk["batches"]["0"]["files"]["Patient.ndjson.gz"]["lines"] == 10
    assert not list(d.glob("manifest.json.tmp"))  # atomic write leaves no temp file


def test_resume_skips_completed_batches_and_regenerates_failed_ones(tmp_path, fhir):
    calls: list[int] = []
    boom = {"on": True}

    def flaky(spec):
        calls.append(spec.index)
        if spec.index == 1 and boom["on"]:
            raise synthea.BatchFailed("simulated JVM crash")
        fhir.write_batch(spec.batch_dir / "fhir", fhir.resources(spec.seed, spec.residents, clinical=False))
        return 1.0

    with pytest.raises(SystemExit):
        fhir.generate(tmp_path, residents=30, batch_size=10, runner=flaky, max_attempts=2, workers=1)
    d = tmp_path / "syn-20261108-s7"
    m = orjson.loads((d / "manifest.json").read_bytes())
    assert (
        m["status"] == "incomplete"
        and m["batches"]["1"]["status"] == "failed"
        and "simulated" in m["batches"]["1"]["error"]
    )
    assert m["batches"]["0"]["status"] == m["batches"]["2"]["status"] == "completed"
    assert calls.count(1) == 2  # one retry within the run

    calls.clear()
    boom["on"] = False
    m2 = fhir.generate(tmp_path, residents=30, batch_size=10, runner=flaky, workers=1)
    assert calls == [1]  # resume ran only the failed batch
    assert m2["status"] == "generated" and m2["totals"]["residents_completed"] == 30


def test_output_is_reproducible_for_a_seed(tmp_path, fhir):
    fhir.generate(tmp_path / "a", residents=10, batch_size=10)
    fhir.generate(tmp_path / "b", residents=10, batch_size=10)
    fa = orjson.loads((tmp_path / "a" / "syn-20261108-s7" / "manifest.json").read_bytes())["batches"]["0"][
        "files"
    ]
    fb = orjson.loads((tmp_path / "b" / "syn-20261108-s7" / "manifest.json").read_bytes())["batches"]["0"][
        "files"
    ]
    assert {k: v["sha256"] for k, v in fa.items()} == {
        k: v["sha256"] for k, v in fb.items()
    }  # gzip is deterministic


def test_wrong_patient_count_fails_the_batch(tmp_path, fhir):
    def short(spec):
        fhir.write_batch(
            spec.batch_dir / "fhir", fhir.resources(spec.seed, spec.residents - 1, clinical=False)
        )
        return 1.0

    with pytest.raises(SystemExit):
        fhir.generate(tmp_path, residents=10, batch_size=10, runner=short, max_attempts=1)
    m = orjson.loads((tmp_path / "syn-20261108-s7" / "manifest.json").read_bytes())
    assert m["batches"]["0"]["status"] == "failed" and "expected 10 Patient" in m["batches"]["0"]["error"]


def test_changed_configuration_needs_a_new_dataset_version(tmp_path, fhir):
    fhir.generate(tmp_path, residents=10, batch_size=10)
    with pytest.raises(SystemExit, match="different configuration"):
        fhir.generate(tmp_path, residents=10, batch_size=5)  # batch size is part of the config hash


def test_on_batch_callback_fires_per_completed_batch(tmp_path, fhir):
    seen: list[int] = []
    fhir.generate(tmp_path, residents=20, batch_size=10, on_batch=lambda i, _e: seen.append(i), workers=1)
    assert sorted(seen) == [0, 1]


def test_resume_adopts_finalised_batches_missing_from_the_manifest(tmp_path, fhir):
    m = fhir.generate(tmp_path, residents=20, batch_size=10)
    d = tmp_path / "syn-20261108-s7"
    original = m["batches"]["1"]["files"]
    lost = orjson.loads((d / "manifest.json").read_bytes())
    del lost["batches"]["1"]  # run died after finalising batch 1 but before the manifest recorded it
    (d / "manifest.json").write_bytes(orjson.dumps(lost))
    calls: list[int] = []
    seen: list[int] = []

    def runner(spec):
        calls.append(spec.index)
        fhir.write_batch(spec.batch_dir / "fhir", fhir.resources(spec.seed, spec.residents))
        return 1.0

    m2 = fhir.generate(
        tmp_path, residents=20, batch_size=10, runner=runner, on_batch=lambda i, _e: seen.append(i)
    )
    assert calls == [] and seen == [1] and m2["status"] == "generated"
    b = m2["batches"]["1"]
    assert b["recovered"] is True and b["residents"] == 10 and b["seed"] == 8
    assert {k: v["sha256"] for k, v in b["files"].items()} == {k: v["sha256"] for k, v in original.items()}
    assert b["resource_counts"]["Patient"] == 10 and sum(b["assignment"]["district"].values()) == 10


def test_half_written_batch_is_regenerated_not_adopted(tmp_path, fhir):
    fhir.generate(tmp_path, residents=10, batch_size=10)
    d = tmp_path / "syn-20261108-s7"
    (d / "batch-0" / "fhir").mkdir()  # raw dir still present => finalisation never completed
    lost = orjson.loads((d / "manifest.json").read_bytes())
    del lost["batches"]["0"]
    (d / "manifest.json").write_bytes(orjson.dumps(lost))
    calls: list[int] = []

    def runner(spec):
        calls.append(spec.index)
        fhir.write_batch(spec.batch_dir / "fhir", fhir.resources(spec.seed, spec.residents))
        return 1.0

    m = fhir.generate(tmp_path, residents=10, batch_size=10, runner=runner)
    assert calls == [0] and not m["batches"]["0"].get("recovered")
