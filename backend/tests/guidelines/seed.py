"""Test-data helpers shared by the guideline and care-gap tests (loaded by path from both conftests).

Everything here is clearly synthetic: codes live in the made-up system `urn:test:synthetic`, and the rule shapes only
exercise the rule format. They are NOT clinical criteria and do not appear in any shipped guideline.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session, sessionmaker

from anticipa.core.audit import Actor
from anticipa.datafabric.models import ClinicalRecord, Dataset, Resident
from anticipa.evidence.models import EvidenceFact
from anticipa.guidelines import service
from anticipa.guidelines.models import GuidelineVersion, RuleVersion
from anticipa.guidelines.population import EvalScope, PopulationStats, evaluate_population

SYS = "urn:test:synthetic"
REVIEWER = Actor(uuid.uuid5(uuid.NAMESPACE_DNS, "clinical_reviewer.anticipa.test"), "clinical_reviewer")


def code(c: str = "PROC-1") -> dict[str, str]:
    return {"system": SYS, "code": c}


def age_leaf(op: str, value: int, leaf_id: str) -> dict[str, Any]:
    return {"id": leaf_id, "field": "resident.age_years", "operator": op, "value": value}


def demo_rule(**overrides: Any) -> dict[str, Any]:
    """Synthetic format-exercise rule: age 40-69 and no completed PROC-1 in the last 5 years (absence = not done)."""
    rule: dict[str, Any] = {
        "care_activity": "demo_activity",
        "priority": "routine",
        "inclusion": {"operator": "AND", "conditions": [age_leaf("GREATER_THAN_OR_EQUAL", 40, "age-min"), age_leaf("LESS_THAN", 70, "age-max")]},
        "exclusions": [
            {
                "id": "recently-done", "outcome": "already_completed", "label": "Completed recently",
                "condition": {
                    "id": "proc-recent", "field": "clinical.procedure", "operator": "EXISTS", "codes": [code()],
                    "lookback": {"years": 5}, "missing_data_policy": "not_satisfied",
                },
            }
        ],
    }
    rule.update(overrides)
    return rule


class Seeder:
    def __init__(self, session: Session) -> None:
        self.s = session
        self._dataset: Dataset | None = None

    @property
    def dataset(self) -> Dataset:
        if self._dataset is None:
            self._dataset = Dataset(dataset_version=f"syn-test-{uuid.uuid4().hex[:8]}", generator="test")
            self.s.add(self._dataset)
            self.s.flush()
        return self._dataset

    def resident(self, birth: date = date(1980, 1, 1), sex: str = "female", deceased: date | None = None, district: str = "D1") -> Resident:
        r = Resident(
            dataset_id=self.dataset.id, source_system="test", source_patient_id=uuid.uuid4().hex, birth_date=birth,
            deceased_date=deceased, administrative_sex=sex, preferred_language="en", district_code=district,
        )
        self.s.add(r)
        self.s.flush()
        return r

    def record(self, resident: Resident, category: str, start: date, c: str = "PROC-1", status: str = "completed") -> ClinicalRecord:
        rec = ClinicalRecord(
            resident_id=resident.id, resource_type=category.capitalize(), source_resource_id=uuid.uuid4().hex, category=category,
            code_system=SYS, code=c, effective_start=datetime(start.year, start.month, start.day, tzinfo=UTC), status=status,
            source_reference=f"test://{category}/{c}",
        )
        self.s.add(rec)
        self.s.flush()
        return rec

    def fact(
        self, resident: Resident, category: str = "family_history", fact_type: str = "demo_condition", subject_type: str = "family_member",
        relationship: str | None = "mother", assertion: str = "positive", temporal: str = "historical", validation: str = "accepted",
        onset: int | None = None,
    ) -> EvidenceFact:  # fmt: skip
        f = EvidenceFact(
            resident_id=resident.id, subject_type=subject_type, category=category, fact_type=fact_type,
            value={"concept": fact_type, **({"age_at_onset": onset} if onset is not None else {})},
            assertion_status=assertion, temporal_status=temporal, relationship_type=relationship, validation_status=validation,
            extraction_method="test",
        )
        self.s.add(f)
        self.s.flush()
        return f

    def approved_version(
        self, rules: dict[str, dict[str, Any]] | None = None, *, guideline_key: str | None = None, effective_from: date = date(2020, 1, 1),
        effective_until: date | None = None, guideline: Any = None, approve: bool = True,
    ) -> tuple[GuidelineVersion, dict[str, RuleVersion]]:  # fmt: skip
        rules = rules if rules is not None else {"demo-rule": demo_rule()}
        g = guideline or service.create_guideline(
            self.s, REVIEWER, guideline_key=guideline_key or f"test-{uuid.uuid4().hex[:10]}", name="Synthetic test guideline", topic="test"
        )
        v = service.create_version(
            self.s, REVIEWER, g, effective_from=effective_from, effective_until=effective_until,
            source_reference="synthetic test fixture - not a clinical source", population_scope={"description": "synthetic"},
            disclaimer="synthetic fixture", rules=[{"rule_key": k, "name": k, "definition": d} for k, d in rules.items()],
        )  # fmt: skip
        if approve:
            service.submit_for_review(self.s, REVIEWER, v)
            service.approve(self.s, REVIEWER, v)
        self.s.flush()
        return v, {r.rule_key: r for r in service.rules_of(self.s, v.id)}


def session_factory(session: Session) -> Callable[[], Session]:
    """New sessions that join the test's rolled-back transaction (what the API and job code open per unit of work)."""
    sm = sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False)
    return sm


def run_evaluation(
    session: Session, rule_versions: list[RuleVersion], on: date, *, resident_ids: list[uuid.UUID] | None = None, mode: str = "incremental",
    chunk_size: int = 1000,
) -> PopulationStats:  # fmt: skip
    return evaluate_population(
        session_factory(session),
        EvalScope(on, [r.id for r in rule_versions], resident_ids=resident_ids, mode=mode, chunk_size=chunk_size),
    )


__all__ = ["REVIEWER", "SYS", "Seeder", "age_leaf", "code", "demo_rule", "insert", "run_evaluation", "session_factory"]
