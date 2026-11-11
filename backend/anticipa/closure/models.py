"""Completion evidence (04 §14.4, 02 §15.3): the only thing that can complete a care-gap case. A message, a response or
an appointment is never evidence. Rows are labelled `simulated` in the MVP (all engagement and capacity data is)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.caregaps import models as _caregap_models  # noqa: F401  (care_gap_cases for the FK)
from anticipa.datafabric.models import SYNTHETIC_STATUSES
from anticipa.db import UUID_PK, Base, sql_in

EVIDENCE_SOURCES = ("simulated_procedure_record", "clinical_record", "document", "manual_attestation")
VERIFICATION_STATUSES = ("pending", "verified", "rejected")
EXCEPTION_TYPES = (
    "resident_declined", "unable_to_contact", "moved_out_of_area", "deceased", "clinically_contraindicated",
    "completed_elsewhere_unverified", "other",
)  # fmt: skip


class CompletionEvidence(Base):
    __tablename__ = "completion_evidence"
    __table_args__ = (
        CheckConstraint(sql_in("source_type", EVIDENCE_SOURCES), name="source_type"),
        CheckConstraint(sql_in("verification_status", VERIFICATION_STATUSES), name="verification_status"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        CheckConstraint(
            "(verification_status = 'verified') = (verified_at IS NOT NULL)", name="verified_at_matches_status"
        ),
        Index("ix_completion_evidence_case", "care_gap_case_id", "created_at"),
        Index("ix_completion_evidence_status", "verification_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    care_gap_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("care_gap_cases.id", ondelete="CASCADE"), nullable=False
    )
    activity: Mapped[str] = mapped_column(Text, nullable=False)  # always equals the case's care activity
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_resource_type: Mapped[str | None] = mapped_column(Text)
    source_resource_id: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))
    evidence_reference: Mapped[str] = mapped_column(Text, nullable=False)
    verification_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending", default="pending")
    verification_method: Mapped[str | None] = mapped_column(Text)  # reference_check | simulated_record | reviewer
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    synthetic_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="simulated", default="simulated")
