"""Invocation accounting. The router writes one record per candidate attempt through an injected sink:
InMemorySink in tests, DbSink (model_invocations table) in the app."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol


@dataclass
class InvocationRecord:
    id: str
    request_id: str
    task: str
    provider: str
    model_id: str
    prompt_version: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0
    retry_count: int = 0
    status: str = "succeeded"  # succeeded | failed
    validation_status: str = "not_applicable"  # valid | repaired | failed | not_applicable
    error_category: str | None = None
    fallback_used: bool = False
    cache_hit: bool = False  # served from the response cache: cost 0, still logged
    job_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_row(self) -> dict[str, Any]:
        """Column dict for the model_invocations table (docs/17 §3). No prompt or document text, ever."""
        row = asdict(self)
        row["id"] = uuid.UUID(self.id)
        try:
            row["job_id"] = uuid.UUID(self.job_id) if self.job_id else None
        except ValueError:
            row["job_id"] = None
        return row


def utc_day_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class InvocationSink(Protocol):
    async def record(self, rec: InvocationRecord) -> None: ...

    async def spent_today_usd(self) -> Decimal:
        """Recorded spend since 00:00 UTC today (read by the router's daily budget guard)."""
        ...

    async def spent_total_usd(self, provider: str | None = None) -> Decimal:
        """Lifetime recorded spend, optionally for one provider (read by the per-provider credit guard)."""
        ...


class InMemorySink:
    def __init__(self, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.records: list[InvocationRecord] = []
        self._now = now  # injectable clock so tests can cross a UTC midnight

    async def spent_today_usd(self) -> Decimal:
        start = utc_day_start(self._now())
        return sum((r.cost_usd for r in self.records if r.created_at >= start), Decimal("0"))

    async def spent_total_usd(self, provider: str | None = None) -> Decimal:
        return sum((r.cost_usd for r in self.records if provider in (None, r.provider)), Decimal("0"))

    async def record(self, rec: InvocationRecord) -> None:
        self.records.append(rec)

    @property
    def total_cost_usd(self) -> Decimal:
        return sum((r.cost_usd for r in self.records), Decimal("0"))


class DbSink:
    """Persists to `model_invocations`. `session_factory` returns a sync SQLAlchemy Session.

    The ORM class lives in anticipa/ai/models.py (foundation workstream) and is imported lazily so this
    module stays importable before that file exists."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def record(self, rec: InvocationRecord) -> None:
        await asyncio.to_thread(self._write, rec)

    async def spent_today_usd(self) -> Decimal:
        return await asyncio.to_thread(self._spent_today)

    async def spent_total_usd(self, provider: str | None = None) -> Decimal:
        return await asyncio.to_thread(self._spent_total, provider)

    def _spent_total(self, provider: str | None) -> Decimal:
        from sqlalchemy import func, select

        from anticipa.ai.models import ModelInvocation  # lazy: owned by the foundation workstream

        stmt = select(func.coalesce(func.sum(ModelInvocation.cost_usd), 0))
        if provider is not None:
            stmt = stmt.where(ModelInvocation.provider == provider)
        with self._session_factory() as session:
            return Decimal(str(session.execute(stmt).scalar_one()))

    def _spent_today(self) -> Decimal:
        from sqlalchemy import func, select

        from anticipa.ai.models import ModelInvocation  # lazy: owned by the foundation workstream

        with self._session_factory() as session:
            total = session.execute(
                select(func.coalesce(func.sum(ModelInvocation.cost_usd), 0)).where(
                    ModelInvocation.created_at >= utc_day_start()
                )
            ).scalar_one()
        return Decimal(str(total))

    def _write(self, rec: InvocationRecord) -> None:
        from anticipa.ai.models import ModelInvocation  # lazy: owned by the foundation workstream

        with self._session_factory() as session:
            session.add(ModelInvocation(**rec.as_row()))
            session.commit()
