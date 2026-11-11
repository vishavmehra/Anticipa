"""Engagement tables (04 §14, 02 §15): approved message templates, SIMULATED communication events, resident responses
and appointments. Nothing here ever reaches a real person: every row is `synthetic_status = 'simulated'` (the schema
rejects anything else) and a "delivery" is a state in this table, not a message sent anywhere.

A planned allocation is a proposal (planning); an `appointments` row is created only from a CONFIRMED allocation, and an
appointment or a message alone never completes a care-gap case (closure needs verified completion evidence).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.caregaps import models as _caregap_models  # noqa: F401  (care_gap_cases / residents for the FKs)
from anticipa.db import UUID_PK, Base, sql_in
from anticipa.planning import models as _planning_models  # noqa: F401  (clinics / slots / allocations for the FKs)

LANGUAGES = ("en", "hi", "ar")
CHANNELS = ("sms", "whatsapp", "phone_call", "email")
DELIVERY_STATES = ("queued", "sent", "delivered", "failed")
RESPONSE_TYPES = ("accept", "decline", "question", "no_response")
APPOINTMENT_STATUSES = ("proposed", "simulated_confirmed", "completed", "no_show", "cancelled")
OPEN_APPOINTMENT_STATUSES = ("proposed", "simulated_confirmed")
SOURCE_TYPES = ("manual", "simulation")
SIMULATED = ("simulated",)  # engagement rows are never `synthetic` (generated data) or real: always simulated


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)


def _simulated() -> Mapped[str]:
    return mapped_column(Text, nullable=False, server_default="simulated", default="simulated")


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CommunicationTemplate(Base):
    """Approved, versioned message text. A version is never edited; a wording change is a new version."""

    __tablename__ = "communication_templates"
    __table_args__ = (
        UniqueConstraint("template_key", "language", "version", name="uq_communication_templates_key_language_version"),
        CheckConstraint(sql_in("language", LANGUAGES), name="language"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    id: Mapped[uuid.UUID] = _pk()
    template_key: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"), default=1)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), default=False)
    created_at: Mapped[datetime] = _created()


class CommunicationEvent(Base):
    __tablename__ = "communication_events"
    __table_args__ = (
        CheckConstraint(sql_in("channel", CHANNELS), name="channel"),
        CheckConstraint(sql_in("language", LANGUAGES), name="language"),
        CheckConstraint(sql_in("delivery_state", DELIVERY_STATES), name="delivery_state"),
        CheckConstraint(sql_in("source_type", SOURCE_TYPES), name="source_type"),
        CheckConstraint(sql_in("synthetic_status", SIMULATED), name="synthetic_status"),
        Index("ix_communication_events_case", "care_gap_case_id", "created_at"),
        Index("ix_communication_events_resident_id", "resident_id"),
        Index("ix_communication_events_state", "delivery_state"),
    )

    id: Mapped[uuid.UUID] = _pk()
    care_gap_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("care_gap_cases.id", ondelete="CASCADE"), nullable=False
    )
    resident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("residents.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("communication_templates.id"))
    template_key: Mapped[str] = mapped_column(Text, nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)  # the rendered, simulated message (no patient identifiers)
    delivery_state: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued", default="queued")
    source_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="manual", default="manual")
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # processing job that produced it (no FK)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()
    synthetic_status: Mapped[str] = _simulated()


class ResidentResponse(Base):
    __tablename__ = "resident_responses"
    __table_args__ = (
        CheckConstraint(sql_in("response_type", RESPONSE_TYPES), name="response_type"),
        CheckConstraint(sql_in("synthetic_status", SIMULATED), name="synthetic_status"),
        Index("ix_resident_responses_event", "communication_event_id"),
        Index("ix_resident_responses_case", "care_gap_case_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    communication_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("communication_events.id", ondelete="CASCADE"), nullable=False
    )
    care_gap_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("care_gap_cases.id", ondelete="CASCADE"), nullable=False
    )
    resident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("residents.id", ondelete="CASCADE"), nullable=False)
    response_type: Mapped[str] = mapped_column(Text, nullable=False)
    response_value: Mapped[str | None] = mapped_column(Text)  # free text of a question; never used for decisions
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    source_reference: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()
    synthetic_status: Mapped[str] = _simulated()


class Appointment(Base):
    """Created from a CONFIRMED planned allocation. `simulated_confirmed` is the strongest state the MVP can reach."""

    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint(sql_in("appointment_status", APPOINTMENT_STATUSES), name="appointment_status"),
        CheckConstraint(sql_in("source_type", SOURCE_TYPES), name="source_type"),
        CheckConstraint(sql_in("synthetic_status", SIMULATED), name="synthetic_status"),
        CheckConstraint("end_time > start_time", name="time_order"),
        UniqueConstraint("planned_allocation_id", name="uq_appointments_planned_allocation"),
        # at most one live (proposed / simulated_confirmed) appointment per case
        Index("uq_appointments_open_case", "care_gap_case_id", unique=True,
              postgresql_where=text("appointment_status IN ('proposed', 'simulated_confirmed')")),  # fmt: skip
        Index("ix_appointments_case", "care_gap_case_id"),
        Index("ix_appointments_status", "appointment_status"),
        Index("ix_appointments_start_time", "start_time"),
    )

    id: Mapped[uuid.UUID] = _pk()
    care_gap_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("care_gap_cases.id", ondelete="CASCADE"), nullable=False
    )
    resident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("residents.id", ondelete="CASCADE"), nullable=False)
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    clinic_service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinic_services.id"), nullable=False)
    capacity_slot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capacity_slots.id"), nullable=False)
    planned_allocation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("planned_allocations.id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    appointment_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed", default="proposed")
    status_reason: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="manual", default="manual")
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    synthetic_status: Mapped[str] = _simulated()
