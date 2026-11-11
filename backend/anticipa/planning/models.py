"""Capacity planning tables (04 §13, contract 17 §2): clinics, services, capacity slots, planning runs, proposed
allocations and unallocated cases. ALL capacity data is simulated (`synthetic_status`); a planned allocation is a
proposal, never an appointment (02 §14.4). Appointments and communications belong to a later wave.

Cross-module references (`care_gap_case_id`, `resident_id`) are plain UUID columns on purpose: the care-gap and resident
tables are owned by other workstreams, and a planning snapshot must stay readable if a case is later closed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.db import UUID_PK, Base, sql_in
from anticipa.planning.domain import REASON_CODES

OPERATING_STATUSES = ("open", "closed")
SLOT_STATUSES = ("open", "closed")
SLOT_SOURCES = ("synthetic_generator", "manual", "api")
RUN_STATUSES = ("queued", "running", "completed", "failed", "cancelled")
ALLOCATION_STATUSES = ("proposed", "confirmed", "superseded")
SYNTHETIC_STATUSES = ("synthetic", "simulated")


def _synthetic() -> Mapped[str]:
    return mapped_column(Text, nullable=False, server_default="simulated", default="simulated")


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)


class Clinic(Base):
    __tablename__ = "clinics"
    __table_args__ = (
        CheckConstraint(sql_in("operating_status", OPERATING_STATUSES), name="operating_status"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        Index("ix_clinics_district_code", "district_code"),
    )

    id: Mapped[uuid.UUID] = _pk()
    clinic_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    district_code: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)  # synthetic or configured
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC", default="UTC")
    operating_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open", default="open")
    synthetic_status: Mapped[str] = _synthetic()
    created_at: Mapped[datetime] = _created()


class ClinicService(Base):
    __tablename__ = "clinic_services"
    __table_args__ = (
        UniqueConstraint("clinic_id", "service_key", name="uq_clinic_services_clinic_service"),
        CheckConstraint("appointment_duration_minutes > 0", name="duration_positive"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False)
    service_key: Mapped[str] = mapped_column(Text, nullable=False)
    service_name: Mapped[str] = mapped_column(Text, nullable=False)
    appointment_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)
    synthetic_status: Mapped[str] = _synthetic()
    created_at: Mapped[datetime] = _created()


class CapacitySlot(Base):
    """`available_capacity` seats at `start_time`; `reserved_capacity` counts CONFIRMED allocations only."""

    __tablename__ = "capacity_slots"
    __table_args__ = (
        CheckConstraint("end_time > start_time", name="time_order"),
        CheckConstraint("available_capacity >= 0", name="available_non_negative"),
        CheckConstraint("reserved_capacity >= 0", name="reserved_non_negative"),
        CheckConstraint("reserved_capacity <= available_capacity", name="reserved_within_available"),
        CheckConstraint(sql_in("slot_status", SLOT_STATUSES), name="slot_status"),
        CheckConstraint(sql_in("source_type", SLOT_SOURCES), name="source_type"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        UniqueConstraint("clinic_service_id", "start_time", name="uq_capacity_slots_service_start"),
        Index("ix_capacity_slots_start_time", "start_time"),
        Index("ix_capacity_slots_clinic_id", "clinic_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False)
    clinic_service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clinic_services.id", ondelete="CASCADE"), nullable=False
    )
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    slot_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open", default="open")
    source_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="manual", default="manual")
    synthetic_status: Mapped[str] = _synthetic()
    created_at: Mapped[datetime] = _created()


class PlanningRun(Base):
    __tablename__ = "planning_runs"
    __table_args__ = (
        CheckConstraint(sql_in("status", RUN_STATUSES), name="status"),
        CheckConstraint("planning_period_end > planning_period_start", name="period_order"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        Index("ix_planning_runs_created_at", "created_at"),
        Index("ix_planning_runs_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    planning_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planning_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    configuration_version: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(Text, nullable=False)
    input_snapshot_hash: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    solver_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued", default="queued")
    case_filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    error_summary: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("processing_jobs.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synthetic_status: Mapped[str] = _synthetic()
    created_at: Mapped[datetime] = _created()


class PlannedAllocation(Base):
    """A PROPOSED placement of one case into one slot. `confirmed` only after `confirm_allocation` re-validated the slot."""

    __tablename__ = "planned_allocations"
    __table_args__ = (
        CheckConstraint(sql_in("allocation_status", ALLOCATION_STATUSES), name="allocation_status"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        UniqueConstraint("planning_run_id", "care_gap_case_id", name="uq_planned_allocations_run_case"),
        Index("ix_planned_allocations_run_status", "planning_run_id", "allocation_status"),
        Index("ix_planned_allocations_slot", "capacity_slot_id"),
        Index("ix_planned_allocations_case", "care_gap_case_id"),
        Index("uq_planned_allocations_confirmed_case", "care_gap_case_id", unique=True,
              postgresql_where=text("allocation_status = 'confirmed'")),  # fmt: skip
    )

    id: Mapped[uuid.UUID] = _pk()
    planning_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("planning_runs.id", ondelete="CASCADE"), nullable=False)
    care_gap_case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    clinic_service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinic_services.id"), nullable=False)
    capacity_slot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capacity_slots.id"), nullable=False)
    clinic_key: Mapped[str] = mapped_column(Text, nullable=False)  # snapshot of what was proposed
    service_key: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str | None] = mapped_column(Text)
    district_code: Mapped[str | None] = mapped_column(Text)  # the resident's district
    language: Mapped[str | None] = mapped_column(Text)
    proposed_start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    proposed_end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    allocation_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed", default="proposed")
    explanation: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    synthetic_status: Mapped[str] = _synthetic()
    created_at: Mapped[datetime] = _created()


class UnallocatedCase(Base):
    """Every plannable case that did not get a slot, with a machine-readable reason (02 §14.3)."""

    __tablename__ = "unallocated_cases"
    __table_args__ = (
        CheckConstraint(sql_in("reason_code", REASON_CODES), name="reason_code"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        UniqueConstraint("planning_run_id", "care_gap_case_id", name="uq_unallocated_cases_run_case"),
        Index("ix_unallocated_cases_run_reason", "planning_run_id", "reason_code"),
    )

    id: Mapped[uuid.UUID] = _pk()
    planning_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("planning_runs.id", ondelete="CASCADE"), nullable=False)
    care_gap_case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_key: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str | None] = mapped_column(Text)
    district_code: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    synthetic_status: Mapped[str] = _synthetic()
    created_at: Mapped[datetime] = _created()
