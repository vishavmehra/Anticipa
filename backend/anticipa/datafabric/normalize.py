"""Streaming normalization of Synthea FHIR R4 bulk NDJSON into `residents` / `clinical_records` rows.

Contract 17 §2, §3, §7 and Docs/04 §5-6. One line at a time (orjson); memory is bounded by the batch's patient-id
map. Invalid resources are written to `<batch_dir>/quarantine.ndjson` with a reason code instead of being loaded.

`clinical_records.category` values: condition, observation, procedure, encounter, diagnostic_report, medication,
immunization, document_reference. `code_system` keeps the original FHIR system URI (Docs/04 §6.3).

Volume control: Observation, Procedure and DiagnosticReport are filtered by the documented allow-lists below
(preventive-relevant codes only); everything else is counted as `skipped`, not quarantined. Condition, Immunization,
MedicationRequest, Encounter (summary fields) and DocumentReference (metadata only, the base64 note is dropped) are
kept in full. CLI (no database needed):
    uv run --directory backend python -m anticipa.datafabric.normalize --dataset-version V [--batch 0]
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import orjson
import psutil

from anticipa.core.ids import resident_uuid
from anticipa.datafabric.assign import assign_district, assign_language
from anticipa.datafabric.synthea import SYNTHEA_DIR, load_manifest, sorted_batches, source_system

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"

# ---- Documented allow-lists (code -> tag stored in value_json["tag"]) ---------------------------------------------
# Codes marked (S) were observed in Synthea v4.0.0 output (100-resident sample); the rest are standard preventive-care
# codes retained so enriched/planted data and future Synthea modules load without code changes. No other codes are
# assumed to be emitted by Synthea (it emits no cervical cytology/HPV/PSA in the sample).
OBSERVATION_ALLOW: dict[str, str] = {
    "39156-5": "bmi",  # (S)
    "85354-9": "blood_pressure",  # (S) panel; systolic/diastolic carried in value_json.components
    "8480-6": "systolic_bp",
    "8462-4": "diastolic_bp",
    "4548-4": "hba1c",  # (S)
    "2339-0": "glucose",  # (S) blood
    "2345-7": "glucose",  # (S) serum/plasma
    "1558-6": "fasting_glucose",
    "2093-3": "total_cholesterol",  # (S)
    "2085-9": "hdl_cholesterol",  # (S)
    "18262-6": "ldl_cholesterol",  # (S) direct assay
    "2089-1": "ldl_cholesterol",
    "13457-7": "ldl_cholesterol",
    "2571-8": "triglycerides",  # (S)
    "72166-2": "smoking_status",  # (S)
    "55758-7": "phq2",  # (S)
    "44261-6": "phq9",  # (S)
    "75626-2": "audit_c",  # (S)
    "57905-2": "fit_fobt",  # (S) stool immunoassay
    "2335-8": "fit_fobt",
    "10524-7": "cervical_cytology",
    "19762-4": "cervical_cytology",
    "47527-7": "cervical_cytology",
    "21440-3": "hpv_test",
    "38265-5": "bone_density",  # (S) DXA T-score
    "2857-1": "psa",
    "55277-8": "hiv_status",  # (S)
    "7917-8": "hiv_antibody",  # (S)
}
PROCEDURE_ALLOW: dict[str, str] = {  # SNOMED CT
    "71651007": "mammography",  # (S)
    "241055006": "mammography_symptomatic",  # (S)
    "73761001": "colonoscopy",  # (S)
    "104435004": "fobt_screening",  # (S)
    "312681000": "bone_density_scan",  # (S)
    "171207006": "depression_screening",  # (S)
    "715252007": "depression_screening",  # (S) PHQ-9
    "763302001": "alcohol_screening",  # (S) AUDIT-C
    "713106006": "drug_abuse_screening",  # (S)
    "866148006": "domestic_abuse_screening",  # (S)
    "171121004": "hiv_screening",  # (S)
    "171122006": "hepatitis_b_screening",  # (S)
    "413107006": "hepatitis_c_screening",  # (S)
    "412809002": "viral_hepatitis_screening",  # (S)
    "171126009": "tb_screening",  # (S)
    "395123002": "diabetes_urine_screening",  # (S)
    "268556000": "diabetes_urine_screening",  # (S)
}
DIAGNOSTIC_REPORT_ALLOW: dict[str, str] = {  # LOINC
    "57698-3": "lipid_panel",  # (S)
    "55757-9": "phq2",  # (S)
    "44249-1": "phq9",  # (S)
    "72109-2": "audit_c",  # (S)
    "82666-9": "dast10",  # (S)
    "75622-1": "hiv_panel",  # (S)
    "10524-7": "cervical_cytology",
}

RESIDENT_COLUMNS = (
    "id", "source_system", "source_patient_id", "birth_date", "deceased_date", "administrative_sex",
    "preferred_language", "district_code", "source_version",
)
CLINICAL_COLUMNS = (
    "resident_id", "resource_type", "source_resource_id", "source_system", "category", "code_system", "code", "display",
    "effective_start", "effective_end", "status", "value_json", "source_reference",
)
CLINICAL_TYPES = (
    "Condition", "Procedure", "Observation", "Immunization", "MedicationRequest", "DiagnosticReport", "Encounter",
    "DocumentReference",
)
_SEX = {"male", "female", "other", "unknown"}


@dataclass
class NormStats:
    read: Counter = field(default_factory=Counter)
    emitted: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)  # valid but not allow-listed
    quarantined: Counter = field(default_factory=Counter)  # reason -> n

    def to_dict(self) -> dict:
        return {k: dict(sorted(getattr(self, k).items())) for k in ("read", "emitted", "skipped", "quarantined")}


class Quarantine:
    """Lazily-created NDJSON of rejected resources with reason codes."""

    def __init__(self, path: Path, stats: NormStats):
        self.path, self.stats, self._fh = path, stats, None
        if path.exists():
            path.unlink()  # a rerun regenerates it

    def add(self, reason: str, rtype: str, rid: str | None, source_file: str, line_no: int, raw: bytes) -> None:
        self.stats.quarantined[reason] += 1
        if self._fh is None:
            self._fh = open(self.path, "wb")  # noqa: SIM115 - closed in close()
        self._fh.write(orjson.dumps({
            "reason": reason, "resource_type": rtype, "id": rid, "file": source_file, "line": line_no,
            "raw": raw[:1000].decode("utf-8", "replace"),
        }) + b"\n")

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


# ---- field helpers ---------------------------------------------------------------------------------------------------
def _ts(value: str | None) -> str | None:
    """ISO 8601 -> UTC ISO string. Naive/date-only values are taken as UTC. Raises ValueError if unparseable."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)).isoformat()


def _cc(cc: dict | None) -> tuple[str | None, str | None, str | None]:
    """(system, code, display) of a CodeableConcept's first coding; display falls back to .text."""
    if not cc:
        return None, None, None
    codings = cc.get("coding") or [{}]
    c = codings[0]
    return c.get("system"), c.get("code"), c.get("display") or cc.get("text")


def _patient_ref(r: dict) -> str | None:
    ref = (r.get("subject") or r.get("patient") or {}).get("reference")
    if not ref:
        return None
    if ref.startswith("Patient/"):
        return ref[8:]
    if ref.startswith("urn:uuid:"):
        return ref[9:]
    return None


def _dumps(d: dict) -> str:
    return orjson.dumps({k: v for k, v in d.items() if v is not None}).decode()


def _first(items: list | None) -> dict:
    return items[0] if items else {}


def _obs_value(r: dict, tag: str) -> str:
    d: dict = {"tag": tag}
    if vq := r.get("valueQuantity"):
        d["value"], d["unit"] = vq.get("value"), vq.get("unit")
    elif vc := r.get("valueCodeableConcept"):
        s, c, disp = _cc(vc)
        d["concept"] = {"system": s, "code": c, "display": disp}
    else:
        for k in ("valueString", "valueBoolean", "valueInteger"):
            if k in r:
                d["value"] = r[k]
                break
    if comps := r.get("component"):
        d["components"] = [
            {"code": _cc(c.get("code"))[1], "value": (c.get("valueQuantity") or {}).get("value"),
             "unit": (c.get("valueQuantity") or {}).get("unit")}
            for c in comps
        ]
    return _dumps(d)


# ---- per-resource mappers: return (category, system, code, display, start, end, status, value_json) | None (skip) |
# ---- str (quarantine reason) ---------------------------------------------------------------------------------------
Mapped = tuple | str | None


def _map_condition(r: dict) -> Mapped:
    s, c, d = _cc(r.get("code"))
    if not c:
        return "missing_code"
    start = _ts(r.get("onsetDateTime") or r.get("recordedDate"))
    if not start:
        return "missing_date"
    status = _cc(r.get("clinicalStatus"))[1]
    value = _dumps({"verification_status": _cc(r.get("verificationStatus"))[1],
                    "category": _cc(_first(r.get("category")))[1]})
    return "condition", s, c, d, start, _ts(r.get("abatementDateTime")), status, value


def _map_observation(r: dict) -> Mapped:
    s, c, d = _cc(r.get("code"))
    if not c:
        return "missing_code"
    tag = OBSERVATION_ALLOW.get(c) if s == LOINC else None
    if tag is None:
        return None
    start = _ts(r.get("effectiveDateTime") or (r.get("effectivePeriod") or {}).get("start") or r.get("issued"))
    if not start:
        return "missing_date"
    return "observation", s, c, d, start, None, r.get("status"), _obs_value(r, tag)


def _map_procedure(r: dict) -> Mapped:
    s, c, d = _cc(r.get("code"))
    if not c:
        return "missing_code"
    tag = PROCEDURE_ALLOW.get(c) if s == SNOMED else None
    if tag is None:
        return None
    per = r.get("performedPeriod") or {}
    start = _ts(r.get("performedDateTime") or per.get("start"))
    if not start:
        return "missing_date"
    return "procedure", s, c, d, start, _ts(per.get("end")), r.get("status"), _dumps({"tag": tag})


def _map_immunization(r: dict) -> Mapped:
    s, c, d = _cc(r.get("vaccineCode"))
    if not c:
        return "missing_code"
    start = _ts(r.get("occurrenceDateTime"))
    if not start:
        return "missing_date"
    return "immunization", s, c, d, start, None, r.get("status"), None


def _map_medication_request(r: dict) -> Mapped:
    s, c, d = _cc(r.get("medicationCodeableConcept"))
    if not c:
        return "missing_code"
    start = _ts(r.get("authoredOn"))
    if not start:
        return "missing_date"
    value = _dumps({"intent": r.get("intent"), "reason": _first(r.get("reasonReference")).get("display")})
    return "medication", s, c, d, start, None, r.get("status"), value


def _map_diagnostic_report(r: dict) -> Mapped:
    s, c, d = _cc(r.get("code"))
    if not c:
        return "missing_code"
    tag = DIAGNOSTIC_REPORT_ALLOW.get(c) if s == LOINC else None
    if tag is None:
        return None
    start = _ts(r.get("effectiveDateTime") or r.get("issued"))
    if not start:
        return "missing_date"
    return "diagnostic_report", s, c, d, start, None, r.get("status"), _dumps({"tag": tag, "results": len(r.get("result") or [])})


def _map_encounter(r: dict) -> Mapped:
    period = r.get("period") or {}
    start = _ts(period.get("start"))
    if not start:
        return "missing_date"
    s, c, d = _cc(_first(r.get("type")))
    value = _dumps({"class": (r.get("class") or {}).get("code"), "reason": _cc(_first(r.get("reasonCode")))[2]})
    return "encounter", s, c, d, start, _ts(period.get("end")), r.get("status"), value


def _map_document_reference(r: dict) -> Mapped:
    start = _ts(r.get("date"))
    if not start:
        return "missing_date"
    s, c, d = _cc(r.get("type"))
    att = _first(r.get("content")).get("attachment") or {}
    value = _dumps({"content_type": att.get("contentType"), "attachment_chars": len(att.get("data") or "")})
    return "document_reference", s, c, d, start, None, r.get("status"), value  # note body deliberately dropped


MAPPERS: dict[str, Callable[[dict], Mapped]] = {
    "Condition": _map_condition, "Procedure": _map_procedure, "Observation": _map_observation,
    "Immunization": _map_immunization, "MedicationRequest": _map_medication_request,
    "DiagnosticReport": _map_diagnostic_report, "Encounter": _map_encounter,
    "DocumentReference": _map_document_reference,
}


def resource_path(batch_dir: Path, rtype: str) -> Path | None:
    for name in (f"{rtype}.ndjson.gz", f"{rtype}.ndjson"):
        if (batch_dir / name).exists():
            return batch_dir / name
    return None


def _lines(path: Path) -> Iterator[bytes]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:
        yield from fh


class BatchNormalizer:
    """residents() must be consumed before clinical_records() (it builds the patient-reference map)."""

    def __init__(self, dataset_version: str, batch_dir: Path, *, source_version: str | None = None):
        self.dataset_version, self.batch_dir = dataset_version, Path(batch_dir)
        self.batch_id = self.batch_dir.name
        self.source_system = source_system(dataset_version)
        self.source_version = source_version
        self.stats = NormStats()
        self.quarantine = Quarantine(self.batch_dir / "quarantine.ndjson", self.stats)
        self._patients: dict[str, uuid.UUID] | None = None

    def close(self) -> None:
        self.quarantine.close()

    def residents(self) -> Iterator[tuple]:
        path = resource_path(self.batch_dir, "Patient")
        if path is None:
            raise FileNotFoundError(f"no Patient.ndjson[.gz] in {self.batch_dir}")
        patients: dict[str, uuid.UUID] = {}
        for n, raw in enumerate(_lines(path), 1):
            self.stats.read["Patient"] += 1
            try:
                r = orjson.loads(raw)
                if not isinstance(r, dict):
                    raise ValueError
            except ValueError:  # orjson.JSONDecodeError subclasses ValueError
                self.quarantine.add("invalid_json", "Patient", None, path.name, n, raw)
                continue
            pid = r.get("id")
            if r.get("resourceType") != "Patient" or not pid:
                self.quarantine.add("missing_id" if r.get("resourceType") == "Patient" else "unexpected_resource_type",
                                    "Patient", pid, path.name, n, raw)
                continue
            if pid in patients:
                self.quarantine.add("duplicate_patient_id", "Patient", pid, path.name, n, raw)
                continue
            try:
                birth = r.get("birthDate")
                if not birth:
                    self.quarantine.add("missing_birth_date", "Patient", pid, path.name, n, raw)
                    continue
                birth_date = date.fromisoformat(birth[:10]).isoformat()
                dec = r.get("deceasedDateTime")
                deceased = _ts(dec)[:10] if dec else None  # type: ignore[index]
            except ValueError:
                self.quarantine.add("invalid_date", "Patient", pid, path.name, n, raw)
                continue
            rid = resident_uuid(self.dataset_version, pid)
            patients[pid] = rid
            gender = r.get("gender")
            self.stats.emitted["Patient"] += 1
            yield (rid, self.source_system, pid, birth_date, deceased, gender if gender in _SEX else "unknown",
                   assign_language(rid), assign_district(rid), self.source_version)
        self._patients = patients

    def clinical_records(self, types: tuple[str, ...] = CLINICAL_TYPES) -> Iterator[tuple]:
        if self._patients is None:
            raise RuntimeError("consume residents() first")
        patients = self._patients
        ref_prefix = f"synthea://{self.dataset_version}/{self.batch_id}/"
        for rtype in types:
            path = resource_path(self.batch_dir, rtype)
            if path is None:
                continue
            mapper = MAPPERS[rtype]
            read, emitted, skipped = self.stats.read, self.stats.emitted, self.stats.skipped
            for n, raw in enumerate(_lines(path), 1):
                read[rtype] += 1
                try:
                    r = orjson.loads(raw)
                except ValueError:
                    self.quarantine.add("invalid_json", rtype, None, path.name, n, raw)
                    continue
                rid = r.get("id") if isinstance(r, dict) else None
                if not rid or r.get("resourceType") != rtype:
                    self.quarantine.add("missing_id" if not rid else "unexpected_resource_type", rtype, rid, path.name, n, raw)
                    continue
                pid = _patient_ref(r)
                if pid is None:
                    self.quarantine.add("unresolvable_patient_reference", rtype, rid, path.name, n, raw)
                    continue
                resident = patients.get(pid)
                if resident is None:
                    self.quarantine.add("unknown_patient", rtype, rid, path.name, n, raw)
                    continue
                try:
                    m = mapper(r)
                except ValueError:
                    self.quarantine.add("invalid_date", rtype, rid, path.name, n, raw)
                    continue
                except (TypeError, AttributeError):
                    self.quarantine.add("malformed_resource", rtype, rid, path.name, n, raw)
                    continue
                if m is None:
                    skipped[rtype] += 1
                    continue
                if isinstance(m, str):
                    self.quarantine.add(m, rtype, rid, path.name, n, raw)
                    continue
                category, system, code, display, start, end, status, value = m
                emitted[rtype] += 1
                yield (resident, rtype, rid, self.source_system, category, system, code, display, start, end, status,
                       value, f"{ref_prefix}{rtype}/{rid}")


# ---- CLI: normalization-only run (no database) for throughput measurement ---------------------------------------------
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m anticipa.datafabric.normalize")
    p.add_argument("--dataset-version", required=True)
    p.add_argument("--batch", type=int, action="append", help="batch index (repeatable); default all completed")
    p.add_argument("--data-dir", type=Path, default=SYNTHEA_DIR)
    a = p.parse_args(argv)
    ds_dir = a.data_dir / a.dataset_version
    manifest = load_manifest(ds_dir)
    if manifest is None:
        raise SystemExit(f"no manifest in {ds_dir}")
    indices = a.batch or [i for i, b in sorted_batches(manifest) if b["status"] == "completed"]
    total = NormStats()
    residents = records = lines = 0
    t0 = time.monotonic()
    for i in indices:
        norm = BatchNormalizer(a.dataset_version, ds_dir / f"batch-{i}", source_version=manifest["generator_version"])
        residents += sum(1 for _ in norm.residents())
        records += sum(1 for _ in norm.clinical_records())
        norm.close()
        for k in ("read", "emitted", "skipped", "quarantined"):
            getattr(total, k).update(getattr(norm.stats, k))
    dt = time.monotonic() - t0
    lines = sum(total.read.values())
    mi = psutil.Process().memory_info()
    print(json.dumps({
        "batches": len(indices), "residents": residents, "clinical_records": records, "lines_read": lines,
        "seconds": round(dt, 2), "lines_per_s": round(lines / dt), "rows_per_s": round(records / dt),
        "peak_rss_mb": round(getattr(mi, "peak_wset", mi.rss) / 2**20, 1), "stats": total.to_dict(),
    }, indent=2))


if __name__ == "__main__":
    main()
