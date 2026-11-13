"""Care-gap tables (04 §12): operational follow-up cases created from eligibility evaluations, plus their
append-only
status history. Status values follow 04 §12.2 (snake_case); the planner-facing ones are
`approved_for_planning` and
`awaiting_capacity` (05 §13.4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.datafabric.models import SYNTHETIC_STATUSES
from anticipa.db import UUID_PK, Base, sql_in
from anticipa.guidelines import models as _guideline_models  # noqa: F401

CASE_STATUSES = (
    "identified",
    "pending_review",
    "approved_for_planning",
    "awaiting_capacity",
    "planned",
    "outreach_pending",
    "outreach_completed",
    "appointment_scheduled",
    "awaiting_completion_evidence",
    "completed",
    "closed_not_completed",
    "escalated",
    "cancelled",
)
TERMINAL_STATUSES = ("completed", "closed_not_completed", "cancelled")
OPEN_STATUSES = tuple(s for s in CASE_STATUSES if s not in TERMINAL_STATUSES)
PRIORITIES = ("routine", "elevated", "high")


def _open_where() -> Any:
    return text("status NOT IN (" + ", ".join(repr(s) for s in TERMINAL_STATUSES) + ")")


class CareGapCase(Base):
    __tablename__ = "care_gap_cases"
    __table_args__ = (
        CheckConstraint(sql_in("status", CASE_STATUSES), name="status"),
        CheckConstraint(sql_in("priority", PRIORITIES), name="priority"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        UniqueConstraint("evaluation_id", name="uq_care_gap_cases_evaluation"),  # one case per evaluation
        # Duplicate prevention (04 §12.4): at most one OPEN case per resident and care activity.
        Index(
            "uq_care_gap_cases_open",
            "resident_id",
            "care_activity",
            unique=True,
            postgresql_where=_open_where(),
        ),
        Index("ix_care_gap_cases_status", "status"),
        Index("ix_care_gap_cases_resident_id", "resident_id"),
        Index("ix_care_gap_cases_activity_status", "care_activity", "status"),
        Index("ix_care_gap_cases_assigned", "assigned_coordinator_id"),
        Index("ix_care_gap_cases_opened_at", "opened_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    resident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("residents.id", ondelete="CASCADE"), nullable=False
    )
    evaluation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eligibility_evaluations.id"), nullable=False)
    guideline_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guideline_versions.id"), nullable=False
    )
    rule_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rule_versions.id")
    )  # decisive rule, if any
    care_activity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="identified", default="identified"
    )
    priority: Mapped[str] = mapped_column(Text, nullable=False, server_default="routine", default="routine")
    source_outcome: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # evaluation outcome that created the case
    assigned_coordinator_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    planning_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # owned by planning (no FK)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closure_reason: Mapped[str | None] = mapped_column(Text)
    synthetic_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="synthetic", default="synthetic"
    )


class CaseStatusHistory(Base):
    """Append-only (DB trigger): every status change of a case, who made it, and why (04 §12.3)."""

    __tablename__ = "case_status_history"
    __table_args__ = (
        CheckConstraint(f"from_status IS NULL OR {sql_in('from_status', CASE_STATUSES)}", name="from_status"),
        CheckConstraint(sql_in("to_status", CASE_STATUSES), name="to_status"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        Index("ix_case_status_history_case", "case_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("care_gap_cases.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_role: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    synthetic_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="synthetic", default="synthetic"
    )
