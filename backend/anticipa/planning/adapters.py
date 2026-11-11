"""Input adapter: open care-gap cases -> `PlannableCase`.

ARCHITECT WIRING POINT. The care-gap workstream owns `anticipa.care_gap_cases` (04 §12.1: id, resident_id,
care_activity, status, priority, opened_at; statuses per 05 §13.4). This adapter reads that table through a minimal
Core projection (only the columns it needs, so extra columns are harmless) joined to `residents` for district and
preferred language. If the real table differs, change `care_gap_cases` below and `CARE_ACTIVITY_SERVICE_KEYS`;
nothing else in the planner depends on it - the solver only ever sees `PlannableCase`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, MetaData, Table, Text, exists, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from anticipa.config import DB_SCHEMA
from anticipa.planning.domain import PlannableCase
from anticipa.planning.models import PlannedAllocation

PLANNABLE_STATUSES = ("approved_for_planning", "awaiting_capacity")  # 05 §13.4

# care_activity (guideline/care-gap vocabulary) -> clinic service_key. Identity unless listed here.
CARE_ACTIVITY_SERVICE_KEYS: dict[str, str] = {}

_projection = MetaData(schema=DB_SCHEMA)  # read-only projections; never used for DDL outside tests
care_gap_cases = Table(
    "care_gap_cases", _projection,
    Column("id", UUID(as_uuid=True), primary_key=True), Column("resident_id", UUID(as_uuid=True)),
    Column("care_activity", Text), Column("status", Text), Column("priority", Text), Column("opened_at", DateTime(timezone=True)),
)  # fmt: skip
residents = Table(
    "residents", _projection,
    Column("id", UUID(as_uuid=True), primary_key=True), Column("district_code", Text), Column("preferred_language", Text),
)  # fmt: skip


class CaseSourceUnavailable(RuntimeError):
    """`care_gap_cases` is missing or does not have the expected columns (adapter not wired yet)."""


class CaseFilters(BaseModel):
    status: list[Literal["approved_for_planning", "awaiting_capacity"]] = Field(default_factory=lambda: list(PLANNABLE_STATUSES))
    district_codes: list[str] | None = None
    care_activities: list[str] | None = None
    priorities: list[str] | None = None
    limit: int | None = Field(None, ge=1)


def load_plannable_cases(session: Session, filters: CaseFilters | Mapping[str, Any] | None = None) -> list[PlannableCase]:
    """Open cases that are approved for planning, have no confirmed allocation yet, ordered by case id."""
    f = filters if isinstance(filters, CaseFilters) else CaseFilters.model_validate(dict(filters or {}))
    c, r = care_gap_cases, residents
    stmt = (
        select(c.c.id, c.c.resident_id, c.c.care_activity, c.c.priority, r.c.district_code, r.c.preferred_language, c.c.opened_at)
        .join(r, r.c.id == c.c.resident_id, isouter=True)
        .where(c.c.status.in_(f.status))
        .where(~exists().where(PlannedAllocation.care_gap_case_id == c.c.id, PlannedAllocation.allocation_status == "confirmed"))
        .order_by(c.c.id)
    )
    if f.district_codes:
        stmt = stmt.where(r.c.district_code.in_(f.district_codes))
    if f.care_activities:
        stmt = stmt.where(c.c.care_activity.in_(f.care_activities))
    if f.priorities:
        stmt = stmt.where(c.c.priority.in_(f.priorities))
    if f.limit:
        stmt = stmt.limit(f.limit)
    try:
        rows = session.execute(stmt).all()
    except ProgrammingError as exc:  # undefined table/column
        session.rollback()
        raise CaseSourceUnavailable(
            "anticipa.care_gap_cases (id, resident_id, care_activity, status, priority, opened_at) is not available; "
            "wire planning/adapters.py to the care-gap schema"
        ) from exc
    return [
        PlannableCase(
            case_id=row.id,
            resident_id=row.resident_id,
            service_key=CARE_ACTIVITY_SERVICE_KEYS.get(row.care_activity or "", row.care_activity or ""),
            priority=row.priority or "",
            district_code=row.district_code or "",
            language=row.preferred_language or "unknown",
            opened_at=row.opened_at,
        )
        for row in rows
    ]
