"""Normalizer + deterministic assignments (no database)."""

from __future__ import annotations

import orjson

from anticipa.core.ids import resident_uuid
from anticipa.datafabric.assign import DISTRICT_CODES, assign_district, assign_language, count_assignments
from anticipa.datafabric.normalize import ALL_TYPES, CLINICAL_TYPES, BatchNormalizer

V = "syn-20261108-s7"


def _run(tmp_path, fhir, resources, *, extra: dict[str, list[bytes]] | None = None, types=CLINICAL_TYPES):
    d = tmp_path / "batch-0"
    fhir.write_batch(d, resources)
    for rtype, lines in (extra or {}).items():  # raw (possibly invalid) lines appended to a resource file
        with open(d / f"{rtype}.ndjson", "ab") as fh:
            fh.write(b"".join(line + b"\n" for line in lines))
    n = BatchNormalizer(V, d, source_version="v-test")
    residents = list(n.residents())
    records = list(n.clinical_records(types))
    n.close()
    return n, residents, records


def test_maps_allow_listed_resources_and_counts_the_rest(tmp_path, fhir):
    res = fhir.resources(1, 6)
    n, residents, records = _run(tmp_path, fhir, res)
    assert len(residents) == 6
    assert (
        n.stats.emitted["Observation"] == 12
    )  # BMI + blood pressure per resident; the other code is skipped
    assert n.stats.skipped["Observation"] == 6 and n.stats.skipped["Procedure"] == 6
    assert not n.stats.quarantined
    by_cat = {c: sum(1 for r in records if r[4] == c) for c in {r[4] for r in records}}
    assert by_cat == {
        "condition": 6,
        "observation": 12,
        "procedure": 6,
        "immunization": 6,
        "medication": 6,
        "diagnostic_report": 6,
        "encounter": 6,
    }
    # record tuples follow CLINICAL_COLUMNS; provenance + original coding preserved
    rec = next(r for r in records if r[2] == res["Condition"][0]["id"])
    assert rec[0] == resident_uuid(V, res["Patient"][0]["id"]) and rec[1] == "Condition"
    assert rec[3] == f"synthea:{V}" and rec[5] == "http://snomed.info/sct" and rec[6] == "44054006"
    assert rec[12] == f"synthea://{V}/batch-0/Condition/{rec[2]}"


def test_observation_values_tags_and_components(tmp_path, fhir):
    _, _, records = _run(tmp_path, fhir, fhir.resources(2, 1))
    bmi = next(r for r in records if r[6] == "39156-5")
    assert orjson.loads(bmi[11]) == {"tag": "bmi", "value": 27.5, "unit": "kg/m2"}
    bp = orjson.loads(next(r for r in records if r[6] == "85354-9")[11])
    assert bp["tag"] == "blood_pressure" and [c["code"] for c in bp["components"]] == ["8480-6", "8462-4"]


def test_document_reference_body_is_dropped(tmp_path, fhir):
    _, _, records = _run(tmp_path, fhir, fhir.resources(3, 1), types=ALL_TYPES)
    doc = next(r for r in records if r[4] == "document_reference")
    assert "note text" not in doc[11] and orjson.loads(doc[11])["attachment_chars"] > 0


def test_quarantine_reason_codes(tmp_path, fhir):
    res = fhir.resources(4, 3)
    pid = res["Patient"][0]["id"]
    bad_obs = [
        b"{not json",
        orjson.dumps({"resourceType": "Observation", "subject": {"reference": f"Patient/{pid}"}}),  # no id
        orjson.dumps({"resourceType": "Observation", "id": "x1", "code": {}}),  # no patient reference
        orjson.dumps(
            {"resourceType": "Observation", "id": "x2", "subject": {"reference": "Patient/not-a-patient"}}
        ),
        orjson.dumps(
            {
                "resourceType": "Observation",
                "id": "x3",
                "subject": {"reference": f"Patient/{pid}"},
                "code": {"coding": [{"system": "http://loinc.org", "code": "39156-5"}]},
            }
        ),  # allow-listed, no date
        orjson.dumps(
            {
                "resourceType": "Observation",
                "id": "x4",
                "subject": {"reference": f"Patient/{pid}"},
                "effectiveDateTime": "not-a-date",
                "code": {"coding": [{"system": "http://loinc.org", "code": "39156-5"}]},
            }
        ),
        orjson.dumps(
            {"resourceType": "Procedure", "id": "x5", "subject": {"reference": f"Patient/{pid}"}}
        ),  # wrong type
    ]
    n, _, records = _run(tmp_path, fhir, res, extra={"Observation": bad_obs})
    q = dict(n.stats.quarantined)
    assert q == {
        "invalid_json": 1,
        "missing_id": 1,
        "unresolvable_patient_reference": 1,
        "unknown_patient": 1,
        "missing_date": 1,
        "invalid_date": 1,
        "unexpected_resource_type": 1,
    }
    lines = (tmp_path / "batch-0" / "quarantine.ndjson").read_text().splitlines()
    assert len(lines) == 7 and {orjson.loads(line)["reason"] for line in lines} == set(q)
    assert len(records) == 3 * 8  # the 27 good rows around the bad lines still load


def test_patient_quarantine_and_unknown_gender(tmp_path, fhir):
    res = fhir.resources(5, 2, clinical=False)
    res["Patient"][0]["gender"] = "nonbinary"
    res["Patient"].append(dict(res["Patient"][1]))  # duplicate id
    res["Patient"].append({"resourceType": "Patient", "id": "nobirth", "gender": "male"})
    res["Patient"].append({"resourceType": "Patient", "id": "badbirth", "birthDate": "2020-13-45"})
    n, residents, _ = _run(tmp_path, fhir, res)
    assert [r[5] for r in residents] == ["unknown", res["Patient"][1]["gender"]]
    assert dict(n.stats.quarantined) == {
        "duplicate_patient_id": 1,
        "missing_birth_date": 1,
        "invalid_date": 1,
    }


def test_assignments_are_deterministic_and_cover_all_codes():
    ids = [resident_uuid(V, f"p{i}") for i in range(3000)]
    assert [assign_district(i) for i in ids[:50]] == [assign_district(i) for i in ids[:50]]
    c = count_assignments(ids)
    assert set(c["district"]) == set(DISTRICT_CODES) and set(c["language"]) == {"en", "hi", "ar"}
    assert abs(c["language"]["en"] / 3000 - 0.60) < 0.04 and abs(c["language"]["ar"] / 3000 - 0.15) < 0.04
    assert assign_language(ids[0]) in {"en", "hi", "ar"}
