# ruff: noqa: E501
"""Resident conversation table (ADR-071): the SIMULATED two way thread between a resident and the engagement assistant,
one thread per care gap case. Nothing here ever reaches a real person: every row is `synthetic_status = 'simulated'`
(the schema rejects anything else). The resident side is entered by a coordinator in the demonstration; the assistant
side is either an approved template or a model reply that passed the deterministic safety checks.

The row stores the resident's text (simulated, so no real PHI) and what was decided about it: the classified intent, the
escalation that was raised and why. Audit events never carry message text, only a hash.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.caregaps import models as _caregap_models  # noqa: F401  (care_gap_cases / residents for the FKs)
from anticipa.db import UUID_PK, Base, sql_in
from anticipa.engagement.models import LANGUAGES

ROLES = ("resident", "assistant")
SOURCES = ("resident_input", "template", "model")
ESCALATION_STATES = ("escalated", "already_escalated", "case_closed")
ESCALATION_REASONS = ("emergency", "medical_question", "eligibility_dispute", "clinician_flag", "unsafe_reply")


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint(sql_in("role", ROLES), name="role"),
        CheckConstraint(sql_in("language", LANGUAGES), name="language"),
        CheckConstraint(sql_in("source", SOURCES), name="source"),
        CheckConstraint(f"escalation_state IS NULL OR {sql_in('escalation_state', ESCALATION_STATES)}", name="escalation_state"),
        CheckConstraint(f"escalation_reason IS NULL OR {sql_in('escalation_reason', ESCALATION_REASONS)}", name="escalation_reason"),
        CheckConstraint(sql_in("synthetic_status", ("simulated",)), name="synthetic_status"),
        Index("ix_conversation_messages_case", "care_gap_case_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    care_gap_case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("care_gap_cases.id", ondelete="CASCADE"), nullable=False)
    resident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("residents.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str | None] = mapped_column(Text)  # classifier label of the resident message this row answers or is
    source: Mapped[str] = mapped_column(Text, nullable=False)
    escalation_state: Mapped[str | None] = mapped_column(Text)  # set on the resident row that raised an escalation
    escalation_reason: Mapped[str | None] = mapped_column(Text)
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # a PROPOSED slot only; never a booking
    model_id: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    invocation_id: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    synthetic_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="simulated", default="simulated")


__all__ = ["ConversationMessage", "ROLES", "SOURCES", "ESCALATION_STATES", "ESCALATION_REASONS"]
