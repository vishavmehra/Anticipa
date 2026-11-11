"""Deterministic identifiers (contract 17 §2). Re-ingesting the same synthetic patient yields the same
UUID."""

from __future__ import annotations

import uuid

ANTICIPA_NS = uuid.UUID("6f1c2a9e-5b7d-4c3e-9a1f-2d8e4b6c0a17")


def uuid5_of(name: str) -> uuid.UUID:
    return uuid.uuid5(ANTICIPA_NS, name)


def resident_uuid(dataset_version: str, synthea_patient_id: str) -> uuid.UUID:
    """UUIDv5 of "{dataset_version}:{synthea_patient_id}" under ANTICIPA_NS."""
    return uuid5_of(f"{dataset_version}:{synthea_patient_id}")
