"""AI-workstream tables (contract 17 §3): one `model_invocations` row per Model Router call (no prompt
text)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.db import UUID_PK, Base, sql_in

ERROR_CATEGORIES = (
    "auth",
    "rate_limited",
    "timeout",
    "unavailable",
    "invalid_request",
    "model_unavailable",
    "context_length",
    "output_validation",
    "refusal",
    "budget_exceeded",
    "unknown",
)  # fmt: skip  (RouterError.category, contract 17 §5)


class ModelInvocation(Base):
    __tablename__ = "model_invocations"
    __table_args__ = (
        CheckConstraint(
            f"error_category IS NULL OR {sql_in('error_category', ERROR_CATEGORIES)}", name="error_category"
        ),
        Index("ix_model_invocations_created_at", "created_at"),
        Index("ix_model_invocations_provider_model", "provider", "model_id"),
    )

    # job_id / request_id are deliberately not foreign keys: tests use an in-memory sink with unpersisted ids.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    request_id: Mapped[str | None] = mapped_column(Text)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # values owned by the AI workstream
    validation_status: Mapped[str | None] = mapped_column(Text)
    error_category: Mapped[str | None] = mapped_column(Text)
    fallback_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    cache_hit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB
    )  # ids only; never document text or prompts
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
