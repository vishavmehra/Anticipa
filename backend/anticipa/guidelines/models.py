"""Guideline engine tables (04 §10-11, 08 §13): guidelines, immutable guideline/rule versions, eligibility
evaluations.

Approved/retired versions are immutable (DB trigger in migration 0002 as well as the service layer);
evaluations are
append-only (a later evaluation never overwrites an earlier one). Synthetic data only: evaluations carry
`synthetic_status` like every other patient-derived table (contract 17 §2).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.datafabric import models as _datafabric_models  # noqa: F401  (residents table for the FKs)
from anticipa.datafabric.models import SYNTHETIC_STATUSES
from anticipa.db import UUID_PK, Base, sql_in
from anticipa.guidelines.evaluator import CLINICAL_REVIEW_STATUSES
from anticipa.guidelines.rules import OUTCOME_CLASSES, OUTCOMES, RULE_LANGUAGE, RULE_TYPES

REVIEW_STATUSES = ("draft", "in_review", "approved", "retired")
EVALUATION_MODES = ("incremental", "full")
CASE_PRIORITIES = ("routine", "elevated", "high")


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Guideline(Base):
    __tablename__ = "guidelines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    guideline_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    topic: Mapped[str | None] = mapped_column(Text)
    authoritative_source: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GuidelineVersion(Base):
    __tablename__ = "guideline_versions"
    __table_args__ = (
        UniqueConstraint("guideline_id", "version", name="uq_guideline_versions_version"),
        CheckConstraint(sql_in("review_status", REVIEW_STATUSES), name="review_status"),
        CheckConstraint(
            "effective_until IS NULL OR effective_until >= effective_from", name="effective_window"
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(sql_in("priority", CASE_PRIORITIES), name="priority"),
        CheckConstraint(
            sql_in("clinical_review_status", CLINICAL_REVIEW_STATUSES), name="clinical_review_status"
        ),
        Index("ix_guideline_versions_review_status", "review_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    guideline_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("guidelines.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    review_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft", default="draft")
    source_reference: Mapped[str | None] = mapped_column(Text)
    source_publication_date: Mapped[date | None] = mapped_column(Date)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_until: Mapped[date | None] = mapped_column(Date)
    population_scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    disclaimer: Mapped[str | None] = mapped_column(
        Text
    )  # free-text review note carried with the version, shown by the UI
    change_summary: Mapped[str | None] = mapped_column(Text)
    care_activity: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # what a case created from this version is about
    priority: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="routine", default="routine"
    )  # case priority
    source_version: Mapped[str | None] = mapped_column(
        Text
    )  # the source document's / draft's own version label
    clinical_review_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending_clinical_review", default="pending_clinical_review"
    )  # copied onto every evaluation; `approved` alone does not mean clinically reviewed
    selectors: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )  # evidence selector library used by the rules
    decision_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    sources: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    version_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )  # everything else the draft says (jurisdiction, domain, notes...), stored verbatim, never executed
    content_hash: Mapped[str | None] = mapped_column(
        Text
    )  # frozen at approval; covers source, window, scope, rules
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _now()
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retirement_reason: Mapped[str | None] = mapped_column(Text)


class RuleVersion(Base):
    __tablename__ = "rule_versions"
    __table_args__ = (
        UniqueConstraint("guideline_version_id", "rule_key", name="uq_rule_versions_key"),
        CheckConstraint(sql_in("approval_status", REVIEW_STATUSES), name="approval_status"),
        CheckConstraint(sql_in("rule_type", RULE_TYPES), name="rule_type"),
        Index("ix_rule_versions_approval_status", "approval_status"),
        Index("ix_rule_versions_care_activity", "care_activity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    guideline_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guideline_versions.id"), nullable=False
    )
    rule_key: Mapped[str] = mapped_column(Text, nullable=False)
    rule_id: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # the rule's own stable id (08 §6.1), e.g. guideline.rule
    rule_type: Mapped[str] = mapped_column(Text, nullable=False)
    precedence: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # the rule's `priority` number (lowest wins)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"), default=1)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    care_activity: Mapped[str] = mapped_column(Text, nullable=False)  # copied from the guideline version
    priority: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="routine", default="routine"
    )  # case priority
    rule_language: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=RULE_LANGUAGE, default=RULE_LANGUAGE
    )
    rule_definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="1", default="1")
    output_schema_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="1", default="1")
    approval_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="draft", default="draft"
    )
    created_at: Mapped[datetime] = _now()


class EligibilityEvaluation(Base):
    """One evaluation of one rule version for one resident. Append-only; `outcome` uses the 04 §11.2
    vocabulary and
    `outcome_class` its 08 §7 class (eligible / ineligible / indeterminate / error)."""

    __tablename__ = "eligibility_evaluations"
    __table_args__ = (
        CheckConstraint(sql_in("outcome", OUTCOMES), name="outcome"),
        CheckConstraint(sql_in("outcome_class", OUTCOME_CLASSES), name="outcome_class"),
        CheckConstraint(sql_in("review_status", CLINICAL_REVIEW_STATUSES), name="review_status"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        Index("ix_eligibility_evaluations_latest", "guideline_version_id", "resident_id", "evaluated_at"),
        Index("ix_eligibility_evaluations_resident_id", "resident_id", "evaluated_at"),
        Index("ix_eligibility_evaluations_outcome", "guideline_version_id", "outcome"),
        Index("ix_eligibility_evaluations_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    resident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("residents.id", ondelete="CASCADE"), nullable=False
    )
    guideline_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guideline_versions.id"), nullable=False
    )
    # the decisive rule version (the criterion behind the outcome); NULL when the resident is eligible
    rule_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("rule_versions.id"))
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    outcome_class: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="pending_clinical_review",
        default="pending_clinical_review",
        # copied from the guideline version at evaluation time (pending clinical review until stated
        # otherwise)
    )
    explanation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_references: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    missing_information: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    conflicting_information: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )  # contradicting sources, all listed; never resolved silently
    source_references: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )  # citations (id/url/publication date/grade) of the rule(s) behind the decision
    exclusion_reasons: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    input_hash: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # input snapshot hash (04 §11.1 input_snapshot_hash)
    evaluation_date: Mapped[date] = mapped_column(Date, nullable=False)
    evaluated_at: Mapped[datetime] = _now()
    execution_version: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )  # no FK: jobs may be pruned independently
    synthetic_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="synthetic", default="synthetic"
    )
