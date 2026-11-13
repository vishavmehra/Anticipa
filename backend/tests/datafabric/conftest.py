"""Fixtures for the data-fabric tests: Synthea-shaped FHIR R4 bulk NDJSON built deterministically in memory.

`fhir.resources(seed, n)` returns {resource_type: [dict, ...]} shaped like Synthea v4.0.0 output (checked against the
syn-20261106-s100 sample); `fhir.write_batch(dir, resources)` writes it as NDJSON; `fhir.generate(...)` drives the real
`synthea.generate` (manifest, gzip, resume) with a fake runner instead of a JVM.
"""
# ruff: noqa: E501, S311

from __future__ import annotations

import base64
import random
import uuid
from datetime import date, timedelta
from pathlib import Path

import orjson
import pytest

from anticipa.datafabric import synthea

LOINC, SNOMED = "http://loinc.org", "http://snomed.info/sct"
REF = date(2026, 11, 8)
ALLOWED_OBS_CODE = "39156-5"  # BMI (allow-listed)
OTHER_OBS_CODE = "99999-9"  # not allow-listed -> counted as skipped
MAMMOGRAPHY = "71651007"
CYTOLOGY = "90226004"  # a guideline-selector code that the allow-lists do not contain


def _cc(system: str, code: str, display: str) -> dict:
    return {"coding": [{"system": system, "code": code, "display": display}], "text": display}


def _ref(pid: str) -> dict:
    return {"reference": f"Patient/{pid}"}


class Fhir:
    REF = REF

    def resources(
        self, seed: int, n: int, *, clinical: bool = True, rules_extra: bool = False
    ) -> dict[str, list[dict]]:
        rng = random.Random(seed)
        out: dict[str, list[dict]] = {
            k: []
            for k in (
                "Patient",
                "Condition",
                "Observation",
                "Procedure",
                "Immunization",
                "MedicationRequest",
                "DiagnosticReport",
                "Encounter",
                "DocumentReference",
            )
        }
        for i in range(n):
            pid = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            born = REF - timedelta(days=rng.randint(0, 90 * 365))
            out["Patient"].append(
                {
                    "resourceType": "Patient",
                    "id": pid,
                    "birthDate": born.isoformat(),
                    "gender": rng.choice(["male", "female"]),
                    "name": [{"family": "Synthetic", "given": [f"P{i}"]}],
                }
            )
            if not clinical:
                continue
            start = (REF - timedelta(days=rng.randint(30, 3000))).isoformat() + "T09:30:00+00:00"
            out["Condition"].append(
                {
                    "resourceType": "Condition",
                    "id": f"{pid[:8]}-c-{i}",
                    "subject": _ref(pid),
                    "onsetDateTime": start,
                    "clinicalStatus": {"coding": [{"code": "active"}]},
                    "verificationStatus": {"coding": [{"code": "confirmed"}]},
                    "category": [
                        _cc(
                            "http://terminology.hl7.org/CodeSystem/condition-category",
                            "encounter-diagnosis",
                            "x",
                        )
                    ],
                    "code": _cc(SNOMED, "44054006", "Diabetes mellitus type 2"),
                }
            )
            out["Observation"].append(
                {
                    "resourceType": "Observation",
                    "id": f"{pid[:8]}-o1-{i}",
                    "subject": _ref(pid),
                    "status": "final",
                    "effectiveDateTime": start,
                    "code": _cc(LOINC, ALLOWED_OBS_CODE, "Body mass index"),
                    "valueQuantity": {"value": 27.5, "unit": "kg/m2"},
                }
            )
            out["Observation"].append(
                {
                    "resourceType": "Observation",
                    "id": f"{pid[:8]}-o2-{i}",
                    "subject": _ref(pid),
                    "status": "final",
                    "effectiveDateTime": start,
                    "code": _cc(LOINC, OTHER_OBS_CODE, "Something else"),
                    "valueQuantity": {"value": 1, "unit": "x"},
                }
            )
            out["Observation"].append(
                {  # blood-pressure panel with components
                    "resourceType": "Observation",
                    "id": f"{pid[:8]}-o3-{i}",
                    "subject": _ref(pid),
                    "status": "final",
                    "effectiveDateTime": start,
                    "code": _cc(LOINC, "85354-9", "Blood pressure panel"),
                    "component": [
                        {
                            "code": _cc(LOINC, "8480-6", "Systolic"),
                            "valueQuantity": {"value": 130, "unit": "mm[Hg]"},
                        },
                        {
                            "code": _cc(LOINC, "8462-4", "Diastolic"),
                            "valueQuantity": {"value": 80, "unit": "mm[Hg]"},
                        },
                    ],
                }
            )
            out["Procedure"].append(
                {
                    "resourceType": "Procedure",
                    "id": f"{pid[:8]}-p1-{i}",
                    "subject": _ref(pid),
                    "status": "completed",
                    "performedPeriod": {"start": start, "end": start},
                    "code": _cc(SNOMED, MAMMOGRAPHY, "Mammography"),
                }
            )
            out["Procedure"].append(
                {
                    "resourceType": "Procedure",
                    "id": f"{pid[:8]}-p2-{i}",
                    "subject": _ref(pid),
                    "status": "completed",
                    "performedPeriod": {"start": start},
                    "code": _cc(SNOMED, "12345678", "Not allow-listed"),
                }
            )
            out["Immunization"].append(
                {
                    "resourceType": "Immunization",
                    "id": f"{pid[:8]}-i-{i}",
                    "patient": _ref(pid),
                    "status": "completed",
                    "occurrenceDateTime": start,
                    "vaccineCode": _cc("http://hl7.org/fhir/sid/cvx", "140", "Influenza"),
                }
            )
            out["MedicationRequest"].append(
                {
                    "resourceType": "MedicationRequest",
                    "id": f"{pid[:8]}-m-{i}",
                    "subject": _ref(pid),
                    "status": "active",
                    "intent": "order",
                    "authoredOn": start,
                    "medicationCodeableConcept": _cc(
                        "http://www.nlm.nih.gov/research/umls/rxnorm", "860975", "Metformin"
                    ),
                }
            )
            out["DiagnosticReport"].append(
                {
                    "resourceType": "DiagnosticReport",
                    "id": f"{pid[:8]}-d-{i}",
                    "subject": _ref(pid),
                    "status": "final",
                    "effectiveDateTime": start,
                    "code": _cc(LOINC, "57698-3", "Lipid panel"),
                    "result": [{"reference": "x"}],
                }
            )
            out["Encounter"].append(
                {
                    "resourceType": "Encounter",
                    "id": f"{pid[:8]}-e-{i}",
                    "subject": _ref(pid),
                    "status": "finished",
                    "class": {"code": "AMB"},
                    "type": [_cc(SNOMED, "185349003", "Encounter for check up")],
                    "period": {"start": start, "end": start},
                }
            )
            out["DocumentReference"].append(
                {
                    "resourceType": "DocumentReference",
                    "id": f"{pid[:8]}-r-{i}",
                    "subject": _ref(pid),
                    "status": "current",
                    "date": start,
                    "type": _cc(LOINC, "34117-2", "History and physical note"),
                    "content": [
                        {
                            "attachment": {
                                "contentType": "text/plain",
                                "data": base64.b64encode(b"note text").decode(),
                            }
                        }
                    ],
                }
            )
            if (
                rules_extra
            ):  # records a guideline selector references, plus an older encounter (latest-only test)
                older = (REF - timedelta(days=4000)).isoformat() + "T09:30:00+00:00"
                sid = f"{pid[:8]}"
                out["Encounter"].append(
                    {
                        "resourceType": "Encounter",
                        "id": f"{sid}-e0-{i}",
                        "subject": _ref(pid),
                        "status": "finished",
                        "class": {"code": "AMB"},
                        "type": [_cc(SNOMED, "185349003", "Encounter for check up")],
                        "period": {"start": older, "end": older},
                    }
                )
                if i % 2 == 0:
                    out["Condition"].append(
                        {
                            "resourceType": "Condition",
                            "id": f"{sid}-c2-{i}",
                            "subject": _ref(pid),
                            "onsetDateTime": start,
                            "clinicalStatus": {"coding": [{"code": "active"}]},
                            "code": _cc(SNOMED, "254837009", "Malignant neoplasm of breast"),
                        }
                    )
                    out["Procedure"].append(
                        {
                            "resourceType": "Procedure",
                            "id": f"{sid}-p3-{i}",
                            "subject": _ref(pid),
                            "status": "completed",
                            "performedPeriod": {"start": start},
                            "code": _cc(
                                SNOMED, CYTOLOGY, "Cytopathology procedure"
                            ),  # rule code, not allow-listed
                        }
                    )
                    out["Observation"].append(
                        {
                            "resourceType": "Observation",
                            "id": f"{sid}-o4-{i}",
                            "subject": _ref(pid),
                            "status": "final",
                            "effectiveDateTime": start,
                            "code": _cc(LOINC, "57905-2", "Hemoglobin in stool by immunoassay"),
                            "valueCodeableConcept": _cc(LOINC, "LA6577-6", "Negative"),
                        }
                    )
                    out["DiagnosticReport"].append(
                        {
                            "resourceType": "DiagnosticReport",
                            "id": f"{sid}-d2-{i}",
                            "subject": _ref(pid),
                            "status": "final",
                            "effectiveDateTime": start,
                            "code": _cc(LOINC, "10524-7", "Cytology report"),
                        }
                    )
        return out

    def write_batch(self, directory: Path, resources: dict[str, list[dict]]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for rtype, items in resources.items():
            (directory / f"{rtype}.ndjson").write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in items))

    def tools(self) -> dict:
        return {
            "java": "java",
            "jar": "x.jar",
            "synthea_tag": "v0-test",
            "jar_sha256": "0" * 64,
            "jre_version": "0",
        }

    def generate(
        self,
        data_dir: Path,
        *,
        residents: int,
        batch_size: int,
        version: str = "syn-20261108-s7",
        seed: int = 7,
        runner=None,
        clinical: bool = True,
        rules_extra: bool = False,
        **kw,
    ) -> dict:
        """Run synthea.generate with a fake runner that writes `batch_size` Synthea-shaped patients per batch."""

        def fake_runner(spec: synthea.BatchSpec) -> float:
            self.write_batch(
                spec.batch_dir / "fhir",
                self.resources(spec.seed, spec.residents, clinical=clinical, rules_extra=rules_extra),
            )
            return 1.0

        return synthea.generate(
            residents=residents,
            batch_size=batch_size,
            base_seed=seed,
            workers=kw.pop("workers", 2),
            dataset_version=version,
            data_dir=data_dir,
            runner=runner or fake_runner,
            tools=self.tools(),
            log=lambda m: None,
            **kw,
        )


@pytest.fixture
def fhir() -> Fhir:
    return Fhir()
