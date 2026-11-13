# ruff: noqa: E501  (long assertion lines)
"""Fixtures for the consent tests: a synthetic resident in the rolled-back test transaction (DB `anticipa_test`)."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from anticipa.datafabric.models import Dataset, Resident


class Residents:
    def __init__(self, session) -> None:
        self.s = session
        self.dataset = Dataset(dataset_version=f"syn-test-{uuid.uuid4().hex[:8]}", generator="test")
        session.add(self.dataset)
        session.flush()

    def make(self, n: int = 1, *, language: str = "en", ids: list[uuid.UUID] | None = None) -> list[Resident]:
        rows = [
            Resident(
                id=(ids[i] if ids else uuid.uuid4()), dataset_id=self.dataset.id, source_system="test",
                source_patient_id=uuid.uuid4().hex, birth_date=date(1970, 1, 1), administrative_sex="female",
                preferred_language=language, district_code="D1",
            )
            for i in range(n)
        ]  # fmt: skip
        self.s.add_all(rows)
        self.s.flush()
        return rows


@pytest.fixture
def residents(db_session) -> Residents:
    return Residents(db_session)


@pytest.fixture
def resident(residents) -> Resident:
    return residents.make()[0]
