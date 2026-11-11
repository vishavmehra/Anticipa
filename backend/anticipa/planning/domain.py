"""Planner domain types: plain dataclasses the solver works on (no ORM, no I/O) and the persisted run configuration.

Everything here is SIMULATED capacity planning over synthetic residents (ADR-003, 02 §14.4): a plan is a proposal,
never a confirmed appointment.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONFIG_VERSION = "planning-config@1"
ALGORITHM_VERSION = "cpsat-grouped-equity@1"

VALUE_UNIT = 1000  # priority weight -> objective value units (keeps integer arithmetic exact)
RATIO_SCALE = 1_000_000  # coverage ratios are modelled in parts-per-million

DEFAULT_PRIORITY_WEIGHTS: dict[str, int] = {"urgent": 8, "high": 4, "medium": 2, "routine": 1, "low": 1}
PRIORITY_ALIASES = {"critical": "urgent", "moderate": "medium", "standard": "medium", "normal": "medium"}

REASON_CODES = (
    "invalid_case_data",
    "service_not_offered",
    "outside_travel_eligibility",
    "no_capacity_in_horizon",
    "capacity_exhausted",
    "resident_time_conflict",
)  # fmt: skip

EquityDimension = Literal["district", "language"]
MAX_HORIZON_DAYS = 92


def normalize_priority(priority: str | None) -> str:
    p = (priority or "").strip().lower()
    return PRIORITY_ALIASES.get(p, p)


class PlanConfig(BaseModel):
    """Persisted verbatim in `planning_runs.config` (with the solver block in `solver_parameters`); a run is
    reproducible from this config + the input snapshot hash. Weights are integers; see docs in `solver.py`."""

    model_config = ConfigDict(frozen=True)

    horizon_start: datetime
    horizon_end: datetime
    max_travel_tier: int = Field(1, ge=0, le=9)  # 0 = own district only; 1 = own + adjacent districts; ...
    priority_weights: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_PRIORITY_WEIGHTS))
    default_priority_weight: int = Field(1, ge=1)
    travel_penalty: int = Field(100, ge=0)  # objective units per travel tier (priority weight unit = 1000)
    equity_weight: float = Field(0.2, ge=0.0, le=1.0)  # per dimension: share of the total priority value at stake
    equity_dimensions: tuple[EquityDimension, ...] = ("district", "language")
    min_group_size: int = Field(30, ge=1)  # groups with fewer plannable cases are reported but not optimised for
    seed: int = 42
    time_limit_seconds: float = Field(30.0, gt=0, le=3600)
    num_workers: int = Field(1, ge=1, le=64)  # 1 => bit-for-bit reproducible CP-SAT search
    relative_gap: float = Field(0.001, ge=0.0, le=1.0)

    @field_validator("horizon_start", "horizon_end")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("must be timezone-aware")
        return v

    @field_validator("priority_weights")
    @classmethod
    def _weights(cls, v: dict[str, int]) -> dict[str, int]:
        if any(w < 1 for w in v.values()):
            raise ValueError("priority weights must be >= 1")
        return {normalize_priority(k): int(w) for k, w in v.items()}

    @model_validator(mode="after")
    def _horizon(self) -> PlanConfig:
        if self.horizon_end <= self.horizon_start:
            raise ValueError("horizon_end must be after horizon_start")
        if self.horizon_end - self.horizon_start > timedelta(days=MAX_HORIZON_DAYS):
            raise ValueError(f"planning horizon is limited to {MAX_HORIZON_DAYS} days")
        return self

    def weight_of(self, priority: str | None) -> int:
        return self.priority_weights.get(normalize_priority(priority), self.default_priority_weight)


@dataclass(frozen=True, slots=True)
class PlannableCase:
    """Planner input for one open care-gap case (see `adapters.load_plannable_cases`)."""

    case_id: uuid.UUID
    resident_id: uuid.UUID
    service_key: str
    priority: str
    district_code: str
    language: str = "unknown"
    opened_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PoolSpec:
    """One clinic service offering (a 'pool' of slots)."""

    clinic_service_id: uuid.UUID
    clinic_id: uuid.UUID
    clinic_key: str
    district_code: str
    service_key: str
    duration_minutes: int
    clinic_open: bool = True
    service_active: bool = True


@dataclass(frozen=True, slots=True)
class SlotSpec:
    """Capacity as seen at plan time: `free` = available_capacity - reserved_capacity."""

    slot_id: uuid.UUID
    clinic_service_id: uuid.UUID
    start: datetime
    end: datetime
    free: int
    status: str = "open"


@dataclass(frozen=True, slots=True)
class Allocation:
    case_id: uuid.UUID
    slot_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Unallocated:
    case_id: uuid.UUID
    reason_code: str
    explanation: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanResult:
    allocations: list[Allocation]
    unallocated: list[Unallocated]
    metrics: dict[str, Any]
    solver: dict[str, Any]
    input_hash: str
    district_order: list[str]
