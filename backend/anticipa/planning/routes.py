"""Planning APIs (05 §13) and the capacity overview (05 §16.3). Mounted under /api/v1 by `anticipa.core.modules`.

Read endpoints need `capacity.view` (the read-only `observer` role has it); anything that changes capacity or confirms an
allocation needs `capacity.manage`; submitting a planning run triggers processing and needs `processing.execute`.
All capacity data is SIMULATED and every payload says so.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from anticipa.api.deps import PageDep, SessionDep
from anticipa.api.envelope import Envelope, PagedEnvelope, PageParams, ok, paged, paginate
from anticipa.core import audit
from anticipa.core.auth import CurrentUser, require_any_permission, require_permission
from anticipa.core.errors import Conflict, NotFound, ValidationFailed
from anticipa.core.rbac import (
    ANALYTICS_VIEW,
    ANALYTICS_VIEW_LIMITED,
    CAPACITY_MANAGE,
    CAPACITY_VIEW,
    PROCESSING_EXECUTE,
)
from anticipa.jobs.service import submit_job
from anticipa.planning import service
from anticipa.planning.adapters import CaseFilters
from anticipa.planning.domain import CONFIG_VERSION, REASON_CODES, PlanConfig
from anticipa.planning.models import (
    ALLOCATION_STATUSES,
    RUN_STATUSES,
    CapacitySlot,
    Clinic,
    ClinicService,
    PlannedAllocation,
    PlanningRun,
    UnallocatedCase,
)

planning = APIRouter(prefix="/planning", tags=["planning"])
analytics = APIRouter(prefix="/analytics", tags=["analytics"])
router = APIRouter()  # the module's single export (core.modules loads `router`)

ViewUser = Annotated[CurrentUser, Depends(require_permission(CAPACITY_VIEW))]
ManageUser = Annotated[CurrentUser, Depends(require_permission(CAPACITY_MANAGE))]
ExecuteUser = Annotated[CurrentUser, Depends(require_permission(PROCESSING_EXECUTE))]
AnalyticsUser = Annotated[CurrentUser, Depends(require_any_permission(ANALYTICS_VIEW, ANALYTICS_VIEW_LIMITED))]
KEY = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


def _aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("must include a timezone (e.g. a trailing Z)")
    return v


def _query_aware(v: datetime | None) -> datetime | None:
    if v is not None and v.tzinfo is None:
        raise ValidationFailed(details=[{"field": "query", "reason": "datetime must include a timezone (e.g. a trailing Z)"}])
    return v


# ----------------------------------------------------------------------------------------------- schemas
class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ServiceOut(_Out):
    id: uuid.UUID
    service_key: str
    service_name: str
    appointment_duration_minutes: int
    active: bool


class ClinicOut(_Out):
    id: uuid.UUID
    clinic_key: str
    name: str
    district_code: str
    address: str | None
    timezone: str
    operating_status: str
    synthetic_status: str
    created_at: datetime
    services: list[ServiceOut] = []


class ServiceIn(BaseModel):
    service_key: str = Field(pattern=KEY)
    service_name: str = Field(min_length=1, max_length=200)
    appointment_duration_minutes: int = Field(ge=5, le=480)
    active: bool = True


class ClinicIn(BaseModel):
    clinic_key: str = Field(pattern=KEY)
    name: str = Field(min_length=1, max_length=200)
    district_code: str = Field(pattern=r"^[A-Za-z0-9_-]{1,16}$")
    address: str | None = Field(None, max_length=300)
    timezone: str = Field("UTC", max_length=64)
    operating_status: Literal["open", "closed"] = "open"
    services: list[ServiceIn] = Field(default_factory=list, max_length=50)


class SlotIn(BaseModel):
    clinic_key: str = Field(pattern=KEY)
    service_key: str = Field(pattern=KEY)
    start_time: datetime
    end_time: datetime
    available_capacity: int = Field(ge=0, le=1000)

    _tz = field_validator("start_time", "end_time")(_aware)

    @model_validator(mode="after")
    def _order(self) -> SlotIn:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class SlotsIn(BaseModel):
    slots: list[SlotIn] = Field(min_length=1, max_length=1000)


class SlotsCreated(BaseModel):
    submitted: int
    created: int
    skipped_existing: int
    synthetic_status: str = "simulated"


class SlotOut(_Out):
    id: uuid.UUID
    clinic_id: uuid.UUID
    clinic_key: str
    clinic_service_id: uuid.UUID
    service_key: str
    start_time: datetime
    end_time: datetime
    available_capacity: int
    reserved_capacity: int
    remaining_capacity: int
    slot_status: str
    source_type: str
    synthetic_status: str


class PlanningPeriod(BaseModel):
    start: datetime
    end: datetime

    _tz = field_validator("start", "end")(_aware)


class EquityConstraints(BaseModel):
    equity_weight: float | None = Field(None, ge=0, le=1)
    dimensions: list[Literal["district", "language"]] | None = None
    min_group_size: int | None = Field(None, ge=1)


class SolverOptions(BaseModel):
    seed: int = 42
    time_limit_seconds: float = Field(30.0, gt=0, le=300)
    num_workers: int = Field(1, ge=1, le=8)  # 1 keeps the run bit-for-bit reproducible


class RunCreate(BaseModel):
    planning_period: PlanningPeriod
    case_filters: CaseFilters = Field(default_factory=CaseFilters)
    objective: Literal["feasible_capacity_allocation"] = "feasible_capacity_allocation"
    equity_constraints: EquityConstraints = Field(default_factory=EquityConstraints)
    max_travel_tier: int = Field(1, ge=0, le=9)
    priority_weights: dict[str, int] | None = None
    solver: SolverOptions = Field(default_factory=SolverOptions)


class RunOut(_Out):
    id: uuid.UUID
    status: str
    planning_period_start: datetime
    planning_period_end: datetime
    configuration_version: str
    algorithm_version: str
    input_snapshot_hash: str | None
    seed: int
    config: dict[str, Any]
    solver_parameters: dict[str, Any]
    case_filters: dict[str, Any]
    metrics: dict[str, Any]
    error_summary: str | None
    job_id: uuid.UUID | None
    created_by: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    synthetic_status: str


class AllocationOut(_Out):
    id: uuid.UUID
    planning_run_id: uuid.UUID
    care_gap_case_id: uuid.UUID
    resident_id: uuid.UUID
    clinic_id: uuid.UUID
    clinic_key: str
    clinic_service_id: uuid.UUID
    service_key: str
    capacity_slot_id: uuid.UUID
    proposed_start_time: datetime
    proposed_end_time: datetime
    allocation_status: str
    priority: str | None
    district_code: str | None
    language: str | None
    explanation: str | None
    confirmed_at: datetime | None
    synthetic_status: str


class UnallocatedOut(_Out):
    id: uuid.UUID
    planning_run_id: uuid.UUID
    care_gap_case_id: uuid.UUID
    resident_id: uuid.UUID
    service_key: str | None
    priority: str | None
    district_code: str | None
    language: str | None
    reason_code: str
    explanation: str
    details: dict[str, Any]
    synthetic_status: str


class ConfirmOut(BaseModel):
    allocation: AllocationOut
    capacity_slot_id: uuid.UUID
    newly_confirmed: bool
    appointment_created: bool = False  # appointments are a later wave: confirmation only reserves capacity
    synthetic_status: str = "simulated"


def _count_page(session, stmt, params: PageParams) -> tuple[list, int]:
    total = session.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar_one()
    return list(session.execute(stmt.limit(params.page_size).offset(params.offset)).all()), total


# ----------------------------------------------------------------------------------------------- clinics
def _clinic_out(session, clinics: list[Clinic]) -> list[ClinicOut]:
    by_clinic: dict[uuid.UUID, list[ClinicService]] = {c.id: [] for c in clinics}
    if clinics:
        for s in session.execute(
            select(ClinicService).where(ClinicService.clinic_id.in_(by_clinic)).order_by(ClinicService.service_key)
        ).scalars():
            by_clinic[s.clinic_id].append(s)
    return [ClinicOut.model_validate(c).model_copy(update={"services": [ServiceOut.model_validate(s) for s in by_clinic[c.id]]})
            for c in clinics]  # fmt: skip


@planning.get("/clinics", response_model=PagedEnvelope[ClinicOut], summary="List simulated clinics and their services")
def list_clinics(
    request: Request, session: SessionDep, user: ViewUser, params: PageDep,
    district_code: Annotated[str | None, Query(max_length=16)] = None,
    operating_status: Annotated[Literal["open", "closed"] | None, Query()] = None,
):  # fmt: skip
    stmt = select(Clinic).order_by(Clinic.district_code, Clinic.clinic_key)
    if district_code:
        stmt = stmt.where(Clinic.district_code == district_code)
    if operating_status:
        stmt = stmt.where(Clinic.operating_status == operating_status)
    rows, total = paginate(session, stmt, params)
    return paged(request, _clinic_out(session, rows), params, total)


@planning.post("/clinics", status_code=201, response_model=Envelope[ClinicOut], summary="Create a simulated clinic")
def create_clinic(body: ClinicIn, request: Request, session: SessionDep, user: ManageUser):
    if session.execute(select(Clinic.id).where(Clinic.clinic_key == body.clinic_key)).first():
        raise Conflict("A clinic with this clinic_key already exists.", [{"field": "clinic_key", "reason": "duplicate"}])
    keys = [s.service_key for s in body.services]
    if len(set(keys)) != len(keys):
        raise ValidationFailed(details=[{"field": "services", "reason": "duplicate service_key"}])
    clinic = Clinic(**body.model_dump(exclude={"services"}), synthetic_status="simulated")
    session.add(clinic)
    session.flush()
    session.add_all(ClinicService(clinic_id=clinic.id, synthetic_status="simulated", **s.model_dump()) for s in body.services)
    audit.record(session, user.actor, "planning.clinic.create", "clinic", clinic.id, "success",
                 {"clinic_key": clinic.clinic_key, "district_code": clinic.district_code, "services": keys})  # fmt: skip
    session.commit()
    return ok(request, _clinic_out(session, [clinic])[0])


# ----------------------------------------------------------------------------------------------- capacity slots
@planning.post("/capacity-slots", status_code=201, response_model=Envelope[SlotsCreated], summary="Register simulated capacity slots")
def register_slots(body: SlotsIn, request: Request, session: SessionDep, user: ManageUser):
    pairs = {(s.clinic_key, s.service_key) for s in body.slots}
    found = {
        (r.clinic_key, r.service_key): r
        for r in session.execute(
            select(Clinic.id.label("clinic_id"), Clinic.clinic_key, ClinicService.id.label("service_id"), ClinicService.service_key)
            .join(ClinicService, ClinicService.clinic_id == Clinic.id)
            .where(Clinic.clinic_key.in_({k for k, _ in pairs}))
        )
    }  # fmt: skip
    missing = [{"field": f"slots.{i}", "reason": f"unknown clinic/service {s.clinic_key}/{s.service_key}"}
               for i, s in enumerate(body.slots) if (s.clinic_key, s.service_key) not in found]  # fmt: skip
    if missing:
        raise ValidationFailed(details=missing[:50])
    rows = {}
    for s in body.slots:  # ponytail: duplicates inside one request collapse to the first
        f = found[s.clinic_key, s.service_key]
        rows.setdefault((f.service_id, s.start_time), {
            "clinic_id": f.clinic_id, "clinic_service_id": f.service_id, "start_time": s.start_time, "end_time": s.end_time,
            "available_capacity": s.available_capacity, "source_type": "api", "synthetic_status": "simulated"})  # fmt: skip
    stmt = pg_insert(CapacitySlot).values(list(rows.values())).on_conflict_do_nothing().returning(CapacitySlot.id)
    created = len(session.execute(stmt).all())
    out = SlotsCreated(submitted=len(body.slots), created=created, skipped_existing=len(body.slots) - created)
    audit.record(session, user.actor, "planning.capacity.register", "capacity_slots", None, "success",
                 {"submitted": out.submitted, "created": out.created, "simulated": True})  # fmt: skip
    session.commit()
    return ok(request, out)


@planning.get("/capacity-slots", response_model=PagedEnvelope[SlotOut], summary="List simulated capacity slots")
def list_slots(
    request: Request, session: SessionDep, user: ViewUser, params: PageDep,
    clinic_key: Annotated[str | None, Query(max_length=64)] = None,
    service_key: Annotated[str | None, Query(max_length=64)] = None,
    district_code: Annotated[str | None, Query(max_length=16)] = None,
    slot_status: Annotated[Literal["open", "closed"] | None, Query()] = None,
    start_from: Annotated[datetime | None, Query(alias="from")] = None,
    start_to: Annotated[datetime | None, Query(alias="to")] = None,
):  # fmt: skip
    stmt = (
        select(CapacitySlot, Clinic.clinic_key, ClinicService.service_key)
        .join(Clinic, Clinic.id == CapacitySlot.clinic_id)
        .join(ClinicService, ClinicService.id == CapacitySlot.clinic_service_id)
        .order_by(CapacitySlot.start_time, Clinic.clinic_key, ClinicService.service_key)
    )
    for cond, value in ((Clinic.clinic_key, clinic_key), (ClinicService.service_key, service_key),
                        (Clinic.district_code, district_code), (CapacitySlot.slot_status, slot_status)):  # fmt: skip
        if value:
            stmt = stmt.where(cond == value)
    if start_from:
        stmt = stmt.where(CapacitySlot.start_time >= _query_aware(start_from))
    if start_to:
        stmt = stmt.where(CapacitySlot.start_time < _query_aware(start_to))
    rows, total = _count_page(session, stmt, params)
    data = [SlotOut(**{c: getattr(s, c) for c in ("id", "clinic_id", "clinic_service_id", "start_time", "end_time", "available_capacity",
                                                  "reserved_capacity", "slot_status", "source_type", "synthetic_status")},
                    clinic_key=ck, service_key=sk, remaining_capacity=s.available_capacity - s.reserved_capacity)
            for s, ck, sk in rows]  # fmt: skip
    return paged(request, data, params, total)


# ----------------------------------------------------------------------------------------------- runs
def _config_from(body: RunCreate) -> PlanConfig:
    kw: dict[str, Any] = {"horizon_start": body.planning_period.start, "horizon_end": body.planning_period.end,
                          "max_travel_tier": body.max_travel_tier, "seed": body.solver.seed,
                          "time_limit_seconds": body.solver.time_limit_seconds, "num_workers": body.solver.num_workers}  # fmt: skip
    eq = body.equity_constraints
    for src, dst in ((eq.equity_weight, "equity_weight"), (eq.min_group_size, "min_group_size"),
                     (tuple(eq.dimensions) if eq.dimensions is not None else None, "equity_dimensions"),
                     (body.priority_weights, "priority_weights")):  # fmt: skip
        if src is not None:
            kw[dst] = src
    try:
        return PlanConfig(**kw)
    except ValidationError as exc:
        raise ValidationFailed(details=[{"field": ".".join(map(str, e["loc"])), "reason": str(e["msg"])} for e in exc.errors()]) from exc


@planning.post("/runs", status_code=202, response_model=Envelope[RunOut], summary="Submit a planning run (async job)")
def submit_run(
    body: RunCreate, request: Request, response: Response, session: SessionDep, user: ExecuteUser,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
):  # fmt: skip
    cfg = _config_from(body)
    run_id = uuid.uuid4()
    submitted = submit_job(session, "planning.run", {"planning_run_id": str(run_id)},
                           f"planning.run:{idempotency_key}" if idempotency_key else None, user.id, config_version=CONFIG_VERSION)  # fmt: skip
    if submitted.created:
        run = service.create_run(session, run_id=run_id, config=cfg, case_filters=body.case_filters,
                                 created_by=user.id, job_id=submitted.job.id)  # fmt: skip
        audit.record(session, user.actor, "planning.run.submit", "planning_run", run.id, "success",
                     {"job_id": str(submitted.job.id), "horizon_start": cfg.horizon_start.isoformat(),
                      "horizon_end": cfg.horizon_end.isoformat(), "seed": cfg.seed})  # fmt: skip
        session.commit()
    else:  # same Idempotency-Key: return the existing run
        run = session.execute(select(PlanningRun).where(PlanningRun.job_id == submitted.job.id)).scalar_one()
        response.status_code = 200
    return ok(request, RunOut.model_validate(run))


def _get_run(session, run_id: uuid.UUID) -> PlanningRun:
    run = session.get(PlanningRun, run_id)
    if run is None:
        raise NotFound("Planning run not found.")
    return run


@planning.get("/runs", response_model=PagedEnvelope[RunOut], summary="List planning runs (newest first; headline metrics only)")
def list_runs(
    request: Request, session: SessionDep, user: ViewUser, params: PageDep,
    status: Annotated[str | None, Query(pattern="^(" + "|".join(RUN_STATUSES) + ")$")] = None,
):  # fmt: skip
    stmt = select(PlanningRun).order_by(PlanningRun.created_at.desc(), PlanningRun.id.desc())
    if status:
        stmt = stmt.where(PlanningRun.status == status)
    rows, total = paginate(session, stmt, params)
    headline = ("cases_total", "allocated", "unallocated", "coverage", "utilization", "reason_counts")
    data = [RunOut.model_validate(r).model_copy(update={"metrics": {k: r.metrics[k] for k in headline if k in r.metrics}}) for r in rows]
    return paged(request, data, params, total)


@planning.get("/runs/{planning_run_id}", response_model=Envelope[RunOut], summary="Get a planning run with full metrics")
def get_run(planning_run_id: uuid.UUID, request: Request, session: SessionDep, user: ViewUser):
    return ok(request, RunOut.model_validate(_get_run(session, planning_run_id)))


@planning.get("/runs/{planning_run_id}/allocations", response_model=PagedEnvelope[AllocationOut],
              summary="Proposed allocations of a run (proposals, not appointments)")  # fmt: skip
def list_allocations(
    planning_run_id: uuid.UUID, request: Request, session: SessionDep, user: ViewUser, params: PageDep,
    allocation_status: Annotated[str | None, Query(pattern="^(" + "|".join(ALLOCATION_STATUSES) + ")$")] = None,
    clinic_key: Annotated[str | None, Query(max_length=64)] = None,
    service_key: Annotated[str | None, Query(max_length=64)] = None,
    district_code: Annotated[str | None, Query(max_length=16)] = None,
    care_gap_case_id: uuid.UUID | None = None,
):  # fmt: skip
    _get_run(session, planning_run_id)
    stmt = select(PlannedAllocation).where(PlannedAllocation.planning_run_id == planning_run_id)
    for col, value in ((PlannedAllocation.allocation_status, allocation_status), (PlannedAllocation.clinic_key, clinic_key),
                       (PlannedAllocation.service_key, service_key), (PlannedAllocation.district_code, district_code),
                       (PlannedAllocation.care_gap_case_id, care_gap_case_id)):  # fmt: skip
        if value:
            stmt = stmt.where(col == value)
    rows, total = paginate(session, stmt.order_by(PlannedAllocation.proposed_start_time, PlannedAllocation.id), params)
    return paged(request, [AllocationOut.model_validate(r) for r in rows], params, total)


@planning.get("/runs/{planning_run_id}/unallocated", response_model=PagedEnvelope[UnallocatedOut],
              summary="Cases the run could not place, each with a reason code and explanation")  # fmt: skip
def list_unallocated(
    planning_run_id: uuid.UUID, request: Request, session: SessionDep, user: ViewUser, params: PageDep,
    reason_code: Annotated[str | None, Query(pattern="^(" + "|".join(REASON_CODES) + ")$")] = None,
    care_gap_case_id: uuid.UUID | None = None,
):  # fmt: skip
    _get_run(session, planning_run_id)
    stmt = select(UnallocatedCase).where(UnallocatedCase.planning_run_id == planning_run_id)
    if reason_code:
        stmt = stmt.where(UnallocatedCase.reason_code == reason_code)
    if care_gap_case_id:
        stmt = stmt.where(UnallocatedCase.care_gap_case_id == care_gap_case_id)
    rows, total = paginate(session, stmt.order_by(UnallocatedCase.reason_code, UnallocatedCase.id), params)
    return paged(request, [UnallocatedOut.model_validate(r) for r in rows], params, total)


@planning.post("/allocations/{allocation_id}/confirm", response_model=Envelope[ConfirmOut],
               summary="Confirm a proposed allocation (re-validates the slot; reserves one seat; no appointment is created)")  # fmt: skip
def confirm_allocation(allocation_id: uuid.UUID, request: Request, session: SessionDep, user: ManageUser):
    alloc, newly = service.confirm_allocation(session, allocation_id, user.actor)
    session.commit()
    return ok(request, ConfirmOut(allocation=AllocationOut.model_validate(alloc), capacity_slot_id=alloc.capacity_slot_id,
                                  newly_confirmed=newly))  # fmt: skip


# ----------------------------------------------------------------------------------------------- analytics
@analytics.get("/capacity", summary="Capacity overview (05 §16.3): total/reserved/remaining seats, utilization, latest plan")
def capacity_overview(
    request: Request, session: SessionDep, user: AnalyticsUser,
    start_from: Annotated[datetime | None, Query(alias="from")] = None,
    start_to: Annotated[datetime | None, Query(alias="to")] = None,
    district_code: Annotated[str | None, Query(max_length=16)] = None,
):  # fmt: skip
    data = service.capacity_overview(session, _query_aware(start_from), _query_aware(start_to), district_code)
    return ok(request, data)


router.include_router(planning)
router.include_router(analytics)
