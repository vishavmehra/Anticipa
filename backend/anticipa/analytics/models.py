"""Precomputed analytics: one row per snapshot key (migration 0008). The heavy aggregates behind the Command
Center, funnel, geography and programme endpoints are computed once (CLI, end of a job or a stale triggered
refresh) and read back in milliseconds. Aggregate counts only: no resident identifiers, no text.
Callers: analytics/snapshot.py (writes), analytics/service.py (reads).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.db import Base


class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    refreshed_by: Mapped[str] = mapped_column(Text, nullable=False, server_default="cli")
