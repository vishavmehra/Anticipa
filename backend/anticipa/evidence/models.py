"""Evidence tables (contract 17 §3, 04 §9, 07 Evidence Engine spec): source-linked facts and their verifiable
sources."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.datafabric.models import SYNTHETIC_STATUSES
from anticipa.db import UUID_PK, Base, sql_in

# Vocabularies follow Docs/07_EVIDENCE_ENGINE_SPEC.md
SUBJECT_TYPES = ("resident", "family_member", "other", "unknown")
ASSERTION_STATUSES = ("positive", "negative", "uncertain", "conditional")
TEMPORAL_STATUSES = ("current", "historical", "planned", "unknown")
EXTRACTION_CONFIDENCES = ("low", "medium", "high")
VALIDATION_STATUSES = (
    "pending", "schema_valid", "source_verified", "accepted", "requires_review", "rejected", "failed",
)  # fmt: skip
VERIFICATION_STATUSES = (
    "verified_exact", "verified_normalized", "span_mismatch", "quote_not_found", "source_missing",
    "verification_error",
)  # fmt: skip


class EvidenceFact(Base):
    __tablename__ = "evidence_facts"
    __table_args__ = (
        CheckConstraint(sql_in("subject_type", SUBJECT_TYPES), name="subject_type"),
        CheckConstraint(sql_in("assertion_status", ASSERTION_STATUSES), name="assertion_status"),
        CheckConstraint(sql_in("temporal_status", TEMPORAL_STATUSES), name="temporal_status"),
        CheckConstraint(
            f"extraction_confidence IS NULL OR {sql_in('extraction_confidence', EXTRACTION_CONFIDENCES)}",
            name="extraction_confidence",
        ),
        CheckConstraint(sql_in("validation_status", VALIDATION_STATUSES), name="validation_status"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
        Index("ix_evidence_facts_resident_category", "resident_id", "category"),
        Index("ix_evidence_facts_validation_status", "validation_status"),
    )

    # invocation_id is not a foreign key: tests use an in-memory InvocationSink with unpersisted ids.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    resident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("residents.id", ondelete="CASCADE"), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="unknown", default="unknown"
    )
    subject_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("residents.id", ondelete="SET NULL")
    )
    category: Mapped[str] = mapped_column(Text, nullable=False)
    fact_type: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    assertion_status: Mapped[str] = mapped_column(Text, nullable=False)
    temporal_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="unknown", default="unknown"
    )
    relationship_type: Mapped[str | None] = mapped_column(Text)
    extraction_confidence: Mapped[str | None] = mapped_column(Text)  # low | medium | high (model self-report)
    uncertainty_note: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3)
    )  # numeric, if ever supported; not clinical prob.
    validation_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending", default="pending"
    )
    review_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="not_required", default="not_required"
    )
    extraction_method: Mapped[str] = mapped_column(Text, nullable=False)
    model_provider: Mapped[str | None] = mapped_column(Text)
    model_id: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    invocation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    synthetic_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="synthetic", default="synthetic"
    )
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvidenceSource(Base):
    __tablename__ = "evidence_sources"
    __table_args__ = (
        CheckConstraint(sql_in("verification_status", VERIFICATION_STATUSES), name="verification_status"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        CheckConstraint(
            "source_end_offset IS NULL OR source_end_offset >= source_start_offset", name="offsets"
        ),
        Index("ix_evidence_sources_evidence_fact_id", "evidence_fact_id"),
        Index("ix_evidence_sources_document_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    evidence_fact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence_facts.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="document", default="document"
    )
    source_resource_type: Mapped[str | None] = mapped_column(Text)
    source_resource_id: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))
    source_field_path: Mapped[str | None] = mapped_column(Text)
    source_quote: Mapped[str | None] = mapped_column(
        Text
    )  # exact text; structured evidence uses field paths instead
    source_start_offset: Mapped[int | None] = mapped_column(Integer)
    source_end_offset: Mapped[int | None] = mapped_column(Integer)
    source_content_hash: Mapped[str | None] = mapped_column(
        Text
    )  # hash of the exact document version verified against
    source_reference: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # no default: the verifier always decides
    location_ambiguous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    all_spans: Mapped[list[Any] | None] = mapped_column(JSONB)  # every matching span when location_ambiguous
    synthetic_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="synthetic", default="synthetic"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
