# ruff: noqa: E501
"""Synthetic resident contexts for the document-generator tests (no database, no Synthea needed)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from anticipa.core.ids import uuid5_of
from anticipa.datafabric.docgen.context import ResidentContext, add_record, finalize

AS_OF = date(2026, 11, 6)
VERSION = "syn-20261106-s42"
SNOMED, LOINC = "http://snomed.info/sct", "http://loinc.org"


def make_context(
    i: int, language: str = "en", sex: str = "female", age: int = 55, records: bool = True
) -> ResidentContext:
    ctx = ResidentContext(
        id=uuid5_of(f"docgen-test:{i}"),
        source_patient_id=f"p{i}",
        birth_date=AS_OF - timedelta(days=int(age * 365.25) + i % 300),
        sex=sex,
        language=language,
        district="D01",
    )
    if records:
        d = lambda years: AS_OF - timedelta(days=int(365.25 * years))  # noqa: E731
        add_record(
            ctx,
            "condition",
            SNOMED,
            "59621000",
            "Essential hypertension (disorder)",
            d(6),
            None,
            "active",
            {},
            f"c{i}a",
        )
        add_record(ctx, "condition", SNOMED, "15777000", "Prediabetes", d(3), None, "active", {}, f"c{i}b")
        add_record(
            ctx,
            "condition",
            SNOMED,
            "160903007",
            "Full-time employment (finding)",
            d(2),
            None,
            "active",
            {},
            f"c{i}c",
        )
        add_record(
            ctx,
            "medication",
            "http://www.nlm.nih.gov/research/umls/rxnorm",
            "314076",
            "lisinopril 10 MG Oral Tablet",
            d(2),
            None,
            "active",
            {},
            f"m{i}",
        )
        bp = {
            "tag": "blood_pressure",
            "components": [
                {"code": "8462-4", "value": 82, "unit": "mm[Hg]"},
                {"code": "8480-6", "value": 131, "unit": "mm[Hg]"},
            ],
        }
        add_record(
            ctx, "observation", LOINC, "85354-9", "Blood pressure panel", d(0.3), None, "final", bp, f"o{i}a"
        )
        add_record(
            ctx,
            "observation",
            LOINC,
            "4548-4",
            "Hemoglobin A1c",
            d(0.3),
            None,
            "final",
            {"tag": "hba1c", "value": 6.1, "unit": "%"},
            f"o{i}b",
        )
        if i % 5 == 0 and age >= 40:
            add_record(
                ctx,
                "procedure",
                SNOMED,
                "73761001",
                "Colonoscopy (procedure)",
                d(4),
                None,
                "completed",
                {"tag": "colonoscopy"},
                f"p{i}",
            )
        add_record(ctx, "encounter", None, None, None, d(0.3), None, None, None, None)
    return finalize(ctx, AS_OF)


@pytest.fixture
def contexts() -> list[ResidentContext]:
    """600 residents, en/hi/ar mixed, ages 3-83, both sexes."""
    langs = ("en", "hi", "ar")
    return [
        make_context(i, langs[i % 3], "female" if i % 2 else "male", 3 + (i * 7) % 81) for i in range(600)
    ]
