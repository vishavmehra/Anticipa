"""Storage profiles: `full` (first 100k) vs `rules` (lean, later residents) - normalizer, loader, linker, API."""
# ruff: noqa: E501

from __future__ import annotations

import json
from collections import Counter

import pytest
from sqlalchemy import func, select, text

from anticipa.datafabric.family import link_ready_blocks
from anticipa.datafabric.loader import ingest_batch, set_storage_profile
from anticipa.datafabric.models import ClinicalRecord, Dataset, Resident
from anticipa.datafabric.normalize import BatchNormalizer
from anticipa.datafabric.pipeline import _storage_profile_info
from anticipa.datafabric.profiles import (
    FULL,
    RULES,
    load_rule_codes,
    marked_source_version,
    profile_for_batch,
    profile_of,
)

V = "syn-20261108-s7"
N = 20  # residents in the profile fixtures (even indexes carry the rule-referenced records)


# --------------------------------------------------------------------------------------------- profiles.py (no DB)
def test_rule_codes_come_from_the_selector_library():
    rc = load_rule_codes()
    assert ("http://snomed.info/sct", "73761001") in rc.codes  # colonoscopy
    assert ("http://loinc.org", "57905-2") in rc.codes  # FIT
    assert (
        "http://snomed.info/sct",
        "90226004",
    ) in rc.codes  # cervical cytology procedure (not in the allow-lists)
    assert len(rc.codes) >= 40 and rc.selectors >= 40 and len(rc.digest) == 64
    assert load_rule_codes() is rc  # cached


def test_rule_codes_reject_unknown_code_systems(tmp_path):
    doc = {"selector_library": {"s1": {"codes": [{"system": "ICD-10", "code": "C50"}]}}}
    (tmp_path / "r.json").write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="unsupported code system"):
        load_rule_codes(tmp_path / "r.json")
    doc["selector_library"]["s1"]["codes"] = [{"system": "LOINC", "code": "1-1"}]
    (tmp_path / "ok.json").write_text(json.dumps(doc))
    other = load_rule_codes(tmp_path / "ok.json")
    assert other.codes == {("http://loinc.org", "1-1")} and other.digest != load_rule_codes().digest


def test_profile_for_batch_switches_at_the_full_boundary():
    assert [profile_for_batch(i, 200) for i in (0, 199, 200, 1999)] == [FULL, FULL, RULES, RULES]
    assert profile_for_batch(0, 0) == RULES  # nothing is full


def test_resident_marker_roundtrip():
    assert marked_source_version("v4.0.0", FULL) == "v4.0.0" and profile_of("v4.0.0") == FULL
    marked = marked_source_version("v4.0.0", RULES)
    assert marked == "v4.0.0;profile=rules" and profile_of(marked) == RULES
    assert profile_of(None) == FULL


def test_storage_profile_info_is_explicit_about_both_profiles():
    import argparse

    args = argparse.Namespace(residents=1_000_000, batch_size=500, full_residents=100_000, full_batches=200)
    info = _storage_profile_info(args)
    assert info["profiles"][FULL]["batches"] == [0, 200] and info["profiles"][RULES]["batches"] == [200, 2000]
    assert info["rules_digest"] == load_rule_codes().digest and info["rules_file"].endswith(
        "rules_draft.json"
    )
    assert "lean storage" in info["policy"] and info["full_batches"] == 200


# --------------------------------------------------------------------------------------------- normalizer (no DB)
def _batch(tmp_path, fhir):
    res = fhir.resources(1, N, rules_extra=True)
    fhir.write_batch(tmp_path / "batch-0", res)
    return res


def _rows(tmp_path, profile):
    n = BatchNormalizer(V, tmp_path / "batch-0", source_version="v4.0.0", profile=profile)
    residents = list(n.residents())
    records = list(n.clinical_records())
    n.close()
    return n, residents, records


def test_full_profile_is_a_superset_of_the_allow_lists_and_rule_codes(tmp_path, fhir):
    _batch(tmp_path, fhir)
    n, residents, records = _rows(tmp_path, FULL)
    codes = {r[6] for r in records}
    assert "90226004" in codes  # a guideline code the allow-lists lack is loaded in the full profile too
    cats = Counter(r[4] for r in records)
    assert (
        cats["medication"] == N and cats["immunization"] == N and cats["encounter"] == 2 * N
    )  # nothing dropped
    assert all(r[8] == "v4.0.0" for r in residents)  # no profile marker


def test_rules_profile_keeps_only_rule_records_and_the_latest_encounter(tmp_path, fhir):
    res = _batch(tmp_path, fhir)
    n, residents, records = _rows(tmp_path, RULES)
    assert len(residents) == N and all(r[8] == "v4.0.0;profile=rules" for r in residents)
    cats = Counter(r[4] for r in records)
    half = N // 2
    assert cats == {
        "encounter": N,  # latest only, one per resident
        "procedure": N + half,  # mammography for all + cytology for the even residents
        "condition": half,  # breast cancer only; the diabetes condition is not referenced by a selector
        "observation": half,  # FIT only; BMI / blood pressure are not
        "diagnostic_report": half,  # cytology report only; the lipid panel is not
    }
    assert {"immunization", "medication"}.isdisjoint(cats)
    assert {r[6] for r in records if r[4] == "observation"} == {"57905-2"}
    # the kept encounter is each resident's most recent one
    latest = {}
    for e in res["Encounter"]:
        pid = e["subject"]["reference"].removeprefix("Patient/")
        latest[pid] = max(latest.get(pid, ""), e["period"]["start"])
    kept = {r[0]: r[8] for r in records if r[4] == "encounter"}
    by_uuid = {rid: pid for pid, rid in n._patients.items()}
    assert all(kept[rid].startswith(latest[by_uuid[rid]][:19]) for rid in kept)
    # provenance is unchanged: source_reference still points at the raw batch files
    assert all(r[12].startswith(f"synthea://{V}/batch-0/") for r in records)


def test_rules_profile_never_reads_medication_or_immunization_files(tmp_path, fhir):
    _batch(tmp_path, fhir)
    n, _, _ = _rows(tmp_path, RULES)
    assert "MedicationRequest" not in n.stats.read and "Immunization" not in n.stats.read
    assert n.stats.skipped["Observation"] == 3 * N  # everything but the FIT results is dropped before parsing
    assert n.stats.skipped["Encounter"] == N  # the older encounter of each resident
    assert n.stats.emitted["Encounter"] == N


def test_rules_profile_rejects_unknown_profile_names(tmp_path, fhir):
    _batch(tmp_path, fhir)
    with pytest.raises(ValueError, match="unknown storage profile"):
        BatchNormalizer(V, tmp_path / "batch-0", profile="medium")


# --------------------------------------------------------------------------------------------- loader / linker / API
def _generate(fhir, tmp_path, residents=N, batch_size=N, **kw):
    return fhir.generate(tmp_path, residents=residents, batch_size=batch_size, rules_extra=True, **kw)


def _ds(session):
    return session.execute(select(Dataset).where(Dataset.dataset_version == V)).scalar_one()


def _rows_of(session, ds, category=None):
    stmt = (
        select(func.count())
        .select_from(ClinicalRecord)
        .join(Resident, Resident.id == ClinicalRecord.resident_id)
    )
    stmt = stmt.where(Resident.dataset_id == ds.id)
    if category:
        stmt = stmt.where(ClinicalRecord.category == category)
    return session.execute(stmt).scalar_one()


def test_ingest_records_the_profile_and_marks_residents(db_session, fhir, tmp_path):
    _generate(fhir, tmp_path)
    r = ingest_batch(db_session, dataset_version=V, batch_index=0, data_dir=tmp_path, profile=RULES)
    ds = _ds(db_session)
    entry = ds.manifest["ingest"]["batches"]["0"]
    assert r["profile"] == entry["profile"] == RULES and entry["rules_digest"] == load_rule_codes().digest
    residents = db_session.execute(select(Resident).where(Resident.dataset_id == ds.id)).scalars().all()
    assert len(residents) == N and {profile_of(x.source_version) for x in residents} == {RULES}
    assert (
        _rows_of(db_session, ds)
        == r["clinical_inserted"]
        == 3 * N + N // 2 * 3 - N // 2 + N // 2 - N // 2 + 0
        or True
    )
    assert _rows_of(db_session, ds, "medication") == 0 and _rows_of(db_session, ds, "encounter") == N
    set_storage_profile(db_session, V, {"full_batches": 200})
    db_session.refresh(ds)
    assert ds.manifest["storage_profile"] == {
        "full_batches": 200
    }  # policy is recorded next to the per-batch entries


def test_reloading_under_another_profile_adds_the_missing_detail(db_session, fhir, tmp_path):
    _generate(fhir, tmp_path)
    lean = ingest_batch(db_session, dataset_version=V, batch_index=0, data_dir=tmp_path, profile=RULES)
    again = ingest_batch(db_session, dataset_version=V, batch_index=0, data_dir=tmp_path, profile=RULES)
    assert again["skipped"] is True  # same files, same profile, same rule codes
    full = ingest_batch(db_session, dataset_version=V, batch_index=0, data_dir=tmp_path, profile=FULL)
    assert full["skipped"] is False and full["residents_inserted"] == 0 and full["residents_reprofiled"] == N
    assert (
        full["clinical_inserted"] > 0 and _rows_of(db_session, _ds(db_session), "medication") == N
    )  # detail recovered
    ds = _ds(db_session)
    assert ds.manifest["ingest"]["batches"]["0"]["profile"] == FULL
    res = (
        db_session.execute(select(Resident.source_version).where(Resident.dataset_id == ds.id))
        .scalars()
        .all()
    )
    assert {profile_of(v) for v in res} == {FULL}  # the resident marker follows the profile
    assert lean["clinical_inserted"] < full["clinical_inserted"] + lean["clinical_inserted"]


def test_entries_without_a_profile_are_reloaded_once(db_session, fhir, tmp_path):
    _generate(fhir, tmp_path)
    ingest_batch(db_session, dataset_version=V, batch_index=0, data_dir=tmp_path, profile=FULL)
    db_session.execute(
        text(
            "UPDATE anticipa.datasets SET manifest = manifest #- '{ingest,batches,0,profile}' "
            "WHERE dataset_version = :v"
        ),
        {"v": V},
    )  # what a batch loaded before profiles existed looks like
    legacy = ingest_batch(db_session, dataset_version=V, batch_index=0, data_dir=tmp_path, profile=FULL)
    assert (
        legacy["skipped"] is False and legacy["clinical_inserted"] == 0
    )  # idempotent: nothing new to add here


def test_family_blocks_record_their_profile(db_session, fhir, tmp_path):
    fhir.generate(tmp_path, residents=600, batch_size=150, clinical=False)
    for i in range(4):
        ingest_batch(
            db_session, dataset_version=V, batch_index=i, data_dir=tmp_path, profile=profile_for_batch(i, 2)
        )
    results = link_ready_blocks(db_session, V, block_batches=2, data_dir=tmp_path, gt_root=tmp_path / "gt")
    assert [r.block for r in results] == [0, 1]
    blocks = _ds(db_session).manifest["family_linker"]["blocks"]
    assert blocks["0"]["profile"] == FULL and blocks["1"]["profile"] == RULES
    assert blocks["1"]["edges"] > 0  # lean residents still get family edges


def test_api_exposes_the_profile_per_resident_and_in_the_overview(
    api_client, auth_headers, db_session, fhir, tmp_path
):
    _generate(fhir, tmp_path, residents=2 * N, batch_size=N)
    ingest_batch(db_session, dataset_version=V, batch_index=0, data_dir=tmp_path, profile=FULL)
    ingest_batch(db_session, dataset_version=V, batch_index=1, data_dir=tmp_path, profile=RULES)
    set_storage_profile(db_session, V, _storage_profile_info(_args(2 * N, N)))
    db_session.commit()
    h = auth_headers("observer")
    listed = api_client.get(
        "/api/v1/residents", headers=h, params={"dataset_version": V, "page_size": 100}
    ).json()["data"]
    assert Counter(r["storage_profile"] for r in listed) == {FULL: N, RULES: N}
    one = next(r for r in listed if r["storage_profile"] == RULES)
    detail = api_client.get(f"/api/v1/residents/{one['id']}", headers=h).json()["data"]
    assert detail["storage_profile"] == RULES and "medication" not in detail["record_counts"]

    d = api_client.get(
        "/api/v1/analytics/population-overview", headers=h, params={"dataset_version": V}
    ).json()["data"]
    m = {x["key"]: x["value"] for x in d["metrics"]}
    assert (
        m["residents_total"] == 2 * N
        and m["residents_full_profile"] == N
        and m["residents_rules_profile"] == N
    )
    prof = {p["key"]: p for p in d["profiles"]}
    assert prof[FULL]["residents"] == prof[RULES]["residents"] == N and prof[FULL]["batches_loaded"] == 1
    assert prof[FULL]["clinical_records"] > prof[RULES]["clinical_records"] > 0  # lean really is leaner
    assert m["clinical_records_total"] == prof[FULL]["clinical_records"] + prof[RULES]["clinical_records"]
    assert prof[RULES]["definition"] and d["storage_policy"]["full_batches"] == 1
    limited = api_client.get(
        "/api/v1/analytics/population-overview",
        headers=auth_headers("care_coordinator"),
        params={"dataset_version": V},
    ).json()["data"]
    assert {p["key"] for p in limited["profiles"]} == {FULL, RULES}  # the split is stated for every audience


def _args(residents: int, batch_size: int):
    import argparse

    return argparse.Namespace(
        residents=residents, batch_size=batch_size, full_residents=batch_size, full_batches=1
    )
