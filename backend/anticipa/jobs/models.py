"""Background job tables (contract 17 §3-4)."""

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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.db import UUID_PK, Base, sql_in

JOB_STATUSES = ("pending", "queued", "running", "partially_completed", "completed", "failed", "cancelled")
TERMINAL_STATUSES = frozenset({"partially_completed", "completed", "failed", "cancelled"})
ITEM_STATUSES = ("pending", "succeeded", "failed", "skipped")


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint(sql_in("status", JOB_STATUSES), name="status"),
        CheckConstraint("max_retries >= 0 AND retry_count >= 0", name="retry_bounds"),
        Index("ix_processing_jobs_claim", "status", "next_run_at", "created_at"),
        Index("ix_processing_jobs_job_type", "job_type"),
        Index("ix_processing_jobs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued", default="queued")
    input_scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    config_version: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    total_items: Mapped[int | None] = mapped_column(Integer)
    processed_items: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    succeeded_items: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"), default=3)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    error_summary: Mapped[str | None] = mapped_column(Text)
    locked_by: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # retry backoff gate
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProcessingItem(Base):
    __tablename__ = "processing_items"
    __table_args__ = (CheckConstraint(sql_in("status", ITEM_STATUSES), name="status"),)

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    item_key: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending", default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
