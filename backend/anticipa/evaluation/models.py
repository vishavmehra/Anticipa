"""`evaluation_runs`: one AGGREGATE row per evaluation run (Docs/09 §10.4, EQ-08). It stores configuration, metric
values, gate results and run-level details; it never stores ground-truth labels, document text or per-case expected
values (only ids of safety-critical cases). Instruction: deliver a working, tested, demonstrable Anticipa MVP
including the evaluation report. Callers: runner.py (writes), routes.py (reads); migration 0007.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.db import UUID_PK, Base, sql_in

RUN_STATUSES = ("running", "complete", "partial", "failed")
RUN_TYPES = ("smoke", "dev", "test_final", "scale")
SPLITS = ("dev", "test", "all", "smoke")


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint(sql_in("status", RUN_STATUSES), name="status"),
        CheckConstraint(sql_in("run_type", RUN_TYPES), name="run_type"),
        CheckConstraint(sql_in("split", SPLITS), name="split"),
        CheckConstraint(sql_in("synthetic_status", ("synthetic", "simulated")), name="synthetic_status"),
        Index("ix_evaluation_runs_created_at", "created_at"),
        Index("ix_evaluation_runs_dataset_version", "dataset_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    run_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # <UTC timestamp>-<8 hex of config hash>
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running", default="running")
    run_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="dev", default="dev")
    dataset_version: Mapped[str] = mapped_column(Text, nullable=False)
    split: Mapped[str] = mapped_column(Text, nullable=False)
    cohort_id: Mapped[str | None] = mapped_column(Text)
    ground_truth_id: Mapped[str | None] = mapped_column(Text)
    metrics_digest: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    prompt_versions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    model_versions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    gates: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    gate_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    error_summary: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # no FK: jobs may be pruned independently
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    synthetic_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="synthetic", default="synthetic")
