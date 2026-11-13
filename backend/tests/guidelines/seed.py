"""Test-data helpers shared by the guideline and care-gap tests (loaded by path from both conftests).

Everything here is clearly synthetic: codes live in the made-up system `urn:test:synthetic`, and the rule
shapes only
exercise the rule format. They are NOT clinical criteria and do not appear in any shipped guideline.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from anticipa.core.audit import Actor
from anticipa.datafabric.models import ClinicalRecord, Dataset, Document, Resident
from anticipa.evidence.models import EvidenceFact, EvidenceSource
from anticipa.guidelines import service
from anticipa.guidelines.models import GuidelineVersion, RuleVersion
from anticipa.guidelines.population import EvalScope, PopulationStats, evaluate_population

SYS = "urn:test:synthetic"
REVIEWER = Actor(uuid.uuid5(uuid.NAMESPACE_DNS, "clinical_reviewer.anticipa.test"), "clinical_reviewer")
FIRST_DEGREE = ["mother", "father", "sister", "brother", "daughter", "son"]
TRUE_PASSES = {"true": "passed", "false": "failed", "unknown": "unknown"}
TRUE_FAILS = {"true": "failed", "false": "passed", "unknown": "unknown"}


def selectors() -> dict[str, Any]:
    return {
        "synth_coverage": {
            "category_in": ["any"],
            "fact_type_in": ["any"],
            "codes": [],
            "subject_type_in": ["resident"],
            "assertion_status_in": ["positive"],
            "temporal_status_in": ["current", "historical"],
            "lookback_years": 5,
        },
        "synth_done": {
            "category_in": ["procedure", "preventive_care"],
            "fact_type_in": ["synthetic_screening"],
            "codes": [{"system": SYS, "code": "PROC-1"}],
            "subject_type_in": ["resident"],
            "assertion_status_in": ["positive"],
            "temporal_status_in": ["current", "historical"],
        },
        "synth_planned": {
            "category_in": ["procedure"],
            "fact_type_in": ["synthetic_screening"],
            "codes": [{"system": SYS, "code": "PROC-1"}],
            "subject_type_in": ["resident"],
            "assertion_status_in": ["positive"],
            "temporal_status_in": ["planned"],
        },
        "synth_excluding_condition": {
            "category_in": ["condition"],
            "fact_type_in": ["synthetic_condition"],
            "codes": [{"system": SYS, "code": "COND-1"}],
            "subject_type_in": ["resident"],
            "assertion_status_in": ["positive"],
            "temporal_status_in": ["current", "historical"],
        },
        "synth_family": {
            "category_in": ["family_history"],
            "fact_type_in": ["synthetic_condition"],
            "codes": [],
            "subject_type_in": ["family_member"],
            "assertion_status_in": ["positive"],
            "temporal_status_in": ["current", "historical", "unknown"],
            "relationship_in": FIRST_DEGREE,
        },
    }


def leaf(field: str, operator: str, value: Any) -> dict[str, Any]:
    return {"field": field, "operator": operator, "value": value}


def rule(
    rid: str,
    rtype: str,
    priority: int,
    expression: dict[str, Any],
    semantics: dict[str, str],
    obr: dict[str, Any],
    reason: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "rule_id": f"demo.{rid}",
        "name": rid.replace("_", " "),
        "rule_type": rtype,
        "priority": priority,
        "expression": expression,
        "criterion_semantics": semantics,
        "outcome_by_expression_result": obr,
        "missing_data_policy": "unknown",
        "reason_code": reason,
        **extra,
    }


INCLUSION_OBR = {"true": None, "false": "not_eligible", "unknown": "insufficient_evidence"}


def when_true(outcome: str) -> dict[str, Any]:
    return {"true": outcome, "false": None, "unknown": "insufficient_evidence"}


def demo_rules() -> list[dict[str, Any]]:
    """Synthetic 08-format rules: coverage prerequisite, age 40-70, personal-history exclusion, completion
    within 5y."""  # noqa: E501
    return [
        rule(
            "record_coverage",
            "prerequisite",
            1,
            leaf("evidence.selector", "EXISTS", "synth_coverage"),
            {"true": "passed", "false": "unknown", "unknown": "unknown"},
            {"true": None, "false": "insufficient_evidence", "unknown": "insufficient_evidence"},
            "no_clinical_record_coverage",
        ),
        rule(
            "age_min",
            "inclusion",
            10,
            leaf("resident.age", "GREATER_THAN_OR_EQUAL", 40),
            {**TRUE_PASSES},
            INCLUSION_OBR,
            "below_minimum_age",
            reason_code_when_unknown="age_not_determinable",
            source_refs=["synthetic_source#E1"],
            source_status="synthetic fixture",
            source_metadata={
                "publication_dates": {"synthetic_source": "2026-01-01"},
                "recommendation_status": {"synthetic_source": "synthetic"},
                "evidence_grade": "n/a",
            },
            explanation_templates={
                "false": "Below the minimum age (synthetic rule).",
                "unknown": "Age could not be determined.",
            },
        ),
        rule(
            "age_max",
            "inclusion",
            11,
            leaf("resident.age", "LESS_THAN_OR_EQUAL", 70),
            {**TRUE_PASSES},
            INCLUSION_OBR,
            "above_maximum_age",
            reason_code_when_unknown="age_not_determinable",
        ),
        rule(
            "excl_condition",
            "exclusion",
            20,
            leaf("evidence.selector", "EXISTS", "synth_excluding_condition"),
            {**TRUE_FAILS},
            when_true("not_eligible"),
            "personal_history_condition",
        ),
        rule(
            "completed_recently",
            "temporal",
            40,
            {
                "operator": "AND",
                "conditions": [
                    leaf("evidence.selector", "EXISTS", "synth_done"),
                    leaf("evidence.synth_done.latest_event_date", "DATE_WITHIN", {"years": 5}),
                ],
            },
            {**TRUE_FAILS},
            when_true("already_completed"),
            "screening_up_to_date",
            reason_code_when_unknown="completion_evidence_indeterminate",
        ),
    ]


def demo_sources() -> list[dict[str, Any]]:
    return [
        {
            "source_id": "synthetic_source",
            "title": "Synthetic fixture - not a clinical source",
            "url": "https://example.invalid/synthetic",
            "publication_date": "2026-01-01",
            "citation": "synthetic",
            "excerpts": [{"id": "E1", "text": "synthetic"}],
        }
    ]


_ACTIVE: dict[
    str, Any
] = {}  # the dataset of the Seeder in use: tests only ever evaluate / count their own residents


def active_dataset_id() -> uuid.UUID:
    return _ACTIVE["id"]


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
            _ACTIVE.update(id=self._dataset.id, version=self._dataset.dataset_version)
        return self._dataset

    def resident(
        self,
        birth: date = date(1980, 1, 1),
        sex: str = "female",
        deceased: date | None = None,
        district: str = "D1",
        covered: bool = True,
    ) -> Resident:
        r = Resident(
            dataset_id=self.dataset.id,
            source_system="test",
            source_patient_id=uuid.uuid4().hex,
            birth_date=birth,
            deceased_date=deceased,
            administrative_sex=sex,
            preferred_language="en",
            district_code=district,
        )
        self.s.add(r)
        self.s.flush()
        if covered:  # a recent structured record: the coverage prerequisite of the demo rules
            self.record(r, "encounter", date(2026, 6, 1), "ENC-1")
        return r

    def record(
        self, resident: Resident, category: str, start: date, c: str = "PROC-1", status: str = "completed"
    ) -> ClinicalRecord:
        rec = ClinicalRecord(
            resident_id=resident.id,
            resource_type=category.capitalize(),
            source_resource_id=uuid.uuid4().hex,
            category=category,
            code_system=SYS,
            code=c,
            effective_start=datetime(start.year, start.month, start.day, tzinfo=UTC),
            status=status,
            source_reference=f"test://{category}/{c}",
        )
        self.s.add(rec)
        self.s.flush()
        return rec

    def document(self, resident: Resident) -> Document:
        d = Document(
            resident_id=resident.id,
            document_type="clinical_note",
            storage_key=f"test/{uuid.uuid4().hex}",
            content_hash=uuid.uuid4().hex,
        )
        self.s.add(d)
        self.s.flush()
        return d

    def fact(
        self,
        resident: Resident,
        category: str = "family_history",
        fact_type: str = "synthetic_condition",
        subject_type: str = "family_member",
        relationship: str | None = "mother",
        assertion: str = "positive",
        temporal: str = "historical",
        validation: str = "accepted",
        onset: int | None = None,
        event_date: str | None = None,
        quote: str | None = None,
        document_id: uuid.UUID | None = None,
    ) -> EvidenceFact:
        value: dict[str, Any] = {"concept": fact_type}
        if onset is not None:
            value["age_at_onset"] = onset
        if event_date is not None:
            value["event_date"] = event_date
        f = EvidenceFact(
            resident_id=resident.id,
            subject_type=subject_type,
            category=category,
            fact_type=fact_type,
            value=value,
            assertion_status=assertion,
            temporal_status=temporal,
            relationship_type=relationship,
            validation_status=validation,
            extraction_method="test",
        )
        self.s.add(f)
        self.s.flush()
        if quote is not None:
            self.s.add(
                EvidenceSource(
                    evidence_fact_id=f.id,
                    document_id=document_id,
                    source_quote=quote,
                    verification_status="verified_exact",
                )
            )
            self.s.flush()
        return f

    def approved_version(
        self,
        rules: list[dict[str, Any]] | None = None,
        *,
        guideline_key: str | None = None,
        effective_from: date = date(2020, 1, 1),
        effective_until: date | None = None,
        guideline: Any = None,
        approve: bool = True,
        selector_library: dict[str, Any] | None = None,
        care_activity: str | None = None,
        clinically_reviewed: bool = False,
    ) -> tuple[GuidelineVersion, dict[str, RuleVersion]]:
        g = guideline or service.create_guideline(
            self.s,
            REVIEWER,
            guideline_key=guideline_key or f"test-{uuid.uuid4().hex[:10]}",
            name="Synthetic test guideline",
            topic="test",
        )
        v = service.create_version(
            self.s,
            REVIEWER,
            g,
            effective_from=effective_from,
            effective_until=effective_until,
            source_reference="synthetic test fixture - not a clinical source",
            population_scope={"description": "synthetic"},
            disclaimer="synthetic fixture",
            care_activity=care_activity or f"demo_{uuid.uuid4().hex[:8]}",
            selectors=selector_library if selector_library is not None else selectors(),
            sources=demo_sources(),
            rules=copy.deepcopy(rules if rules is not None else demo_rules()),
        )
        if approve:
            service.submit_for_review(self.s, REVIEWER, v)
            service.approve(self.s, REVIEWER, v, clinically_reviewed=clinically_reviewed)
        self.s.flush()
        return v, {r.rule_key: r for r in service.rules_of(self.s, v.id)}


def session_factory(session: Session) -> Callable[[], Session]:
    """New sessions that join the test's rolled-back transaction (what the API and job code open per unit of
    work)."""  # noqa: E501
    return sessionmaker(
        bind=session.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def run_evaluation(
    session: Session,
    versions: list[GuidelineVersion],
    on: date,
    *,
    resident_ids: list[uuid.UUID] | None = None,
    mode: str = "incremental",
    chunk_size: int = 1000,
    dataset_version: str | None = None,
) -> PopulationStats:
    """Evaluate the active seeder's dataset only (the shared test database may hold other workstreams'
    committed rows)."""  # noqa: E501
    return evaluate_population(
        session_factory(session),
        EvalScope(
            on,
            [v.id for v in versions],
            dataset_version=dataset_version or _ACTIVE.get("version"),
            resident_ids=resident_ids,
            mode=mode,
            chunk_size=chunk_size,
        ),
    )
