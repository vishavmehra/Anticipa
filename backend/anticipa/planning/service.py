"""Planning workflows over the database: capacity loading, run execution, plan persistence, allocation confirmation
and the capacity overview. Routes and job handlers stay thin and call these functions.

A plan is a proposal. `confirm_allocation` is the only place capacity is reserved, and it re-validates the slot first
(05 §13.7, 02 §14.4); appointment records are a later wave.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.core.errors import Conflict, NotFound
from anticipa.core.ids import uuid5_of
from anticipa.jobs.context import JobCancelled, JobContext, short_error
from anticipa.planning.adapters import CaseFilters, load_plannable_cases
from anticipa.planning.domain import (
    ALGORITHM_VERSION,
    CONFIG_VERSION,
    PlanConfig,
    PlannableCase,
    PoolSpec,
    SlotSpec,
)
from anticipa.planning.models import (
    CapacitySlot,
    Clinic,
    ClinicService,
    PlannedAllocation,
    PlanningRun,
    UnallocatedCase,
)
from anticipa.planning.solver import TravelModel, plan
from anticipa.planning.validate import HARD_CONSTRAINTS, Violation, validate_plan

log = logging.getLogger(__name__)


class PlanValidationError(RuntimeError):
    def __init__(self, violations: Sequence[Violation]) -> None:
        super().__init__(f"plan violates {len(violations)} hard constraint(s): "
                         + ", ".join(sorted({v.constraint for v in violations})))  # fmt: skip
        self.violations = list(violations)


# ------------------------------------------------------------------------------------------------ capacity input
def load_capacity(session: Session, start: datetime, end: datetime) -> tuple[list[PoolSpec], list[SlotSpec]]:
    """All clinic service pools, and the slots that lie inside [start, end]."""
    pools = [
        PoolSpec(r.id, r.clinic_id, r.clinic_key, r.district_code, r.service_key, r.appointment_duration_minutes,
                 r.operating_status == "open", r.active)
        for r in session.execute(
            select(ClinicService.id, ClinicService.clinic_id, Clinic.clinic_key, Clinic.district_code, ClinicService.service_key,
                   ClinicService.appointment_duration_minutes, Clinic.operating_status, ClinicService.active)
            .join(Clinic, Clinic.id == ClinicService.clinic_id)
        )  # fmt: skip
    ]
    slots = [
        SlotSpec(r.id, r.clinic_service_id, r.start_time, r.end_time, r.available_capacity - r.reserved_capacity, r.slot_status)
        for r in session.execute(
            select(CapacitySlot.id, CapacitySlot.clinic_service_id, CapacitySlot.start_time, CapacitySlot.end_time,
                   CapacitySlot.available_capacity, CapacitySlot.reserved_capacity, CapacitySlot.slot_status)
            .where(CapacitySlot.start_time >= start, CapacitySlot.end_time <= end)
        )  # fmt: skip
    ]
    return pools, slots


# ------------------------------------------------------------------------------------------------ runs
def create_run(session: Session, *, run_id: uuid.UUID, config: PlanConfig, case_filters: CaseFilters,
               created_by: uuid.UUID | None, job_id: uuid.UUID | None) -> PlanningRun:  # fmt: skip
    run = PlanningRun(
        id=run_id,
        planning_period_start=config.horizon_start,
        planning_period_end=config.horizon_end,
        configuration_version=CONFIG_VERSION,
        algorithm_version=ALGORITHM_VERSION,
        config=config.model_dump(mode="json"),
        solver_parameters={"seed": config.seed, "num_workers": config.num_workers, "time_limit_seconds": config.time_limit_seconds,
                           "relative_gap_limit": config.relative_gap},  # fmt: skip
        seed=config.seed,
        status="queued",
        case_filters=case_filters.model_dump(mode="json"),
        created_by=created_by,
        job_id=job_id,
    )
    session.add(run)
    session.flush()
    return run


def _set_run_state(sessions: Callable[[], Session], run_id: uuid.UUID, status: str, error: str | None, details: dict[str, Any]) -> None:
    with sessions() as s:
        run = s.get(PlanningRun, run_id, with_for_update=True)
        if run is None:
            return
        run.status, run.error_summary, run.completed_at = status, error, datetime.now(UTC)
        audit.record(s, Actor.system(), f"planning.run.{status}", "planning_run", run_id,
                     "success" if status == "cancelled" else "failure", details)  # fmt: skip
        s.commit()


def execute_run(sessions: Callable[[], Session], run_id: uuid.UUID, ctx: JobContext | None = None) -> dict[str, Any]:
    """Plan, validate, persist. Idempotent: a completed run is returned as is; a retried run rewrites its own rows
    in one transaction, so a crash can never leave a half-written plan. Returns the run metrics."""
    with sessions() as s:
        run = s.get(PlanningRun, run_id, with_for_update=True)
        if run is None:
            raise NotFound("Planning run not found.")
        if run.status == "completed":
            return dict(run.metrics)
        run.status, run.started_at, run.error_summary, run.completed_at = "running", datetime.now(UTC), None, None
        cfg = PlanConfig.model_validate(run.config)
        filters = CaseFilters.model_validate(run.case_filters)
        s.commit()
    try:
        timings: dict[str, float] = {}
        t = time.perf_counter()
        with sessions() as s:
            cases = load_plannable_cases(s, filters)
            pools, slots = load_capacity(s, cfg.horizon_start, cfg.horizon_end)
        timings["load"] = time.perf_counter() - t
        if ctx:
            ctx.progress(total=len(cases))
            ctx.raise_if_cancelled()
        result = plan(cases, pools, slots, cfg)
        t = time.perf_counter()
        violations = validate_plan(result.allocations, cases, pools, slots, cfg)
        timings["validate"] = time.perf_counter() - t
        if violations:  # never publish a plan that breaks a hard constraint (ADR-020)
            raise PlanValidationError(violations)
        if ctx:
            ctx.raise_if_cancelled()
            ctx.checkpoint({"stage": "persisting", "cases": len(cases), "allocated": len(result.allocations)})
        t = time.perf_counter()
        with sessions() as s:
            run = s.get(PlanningRun, run_id, with_for_update=True)
            assert run is not None
            _persist(s, run, cases, pools, slots, result)
            timings["persist"] = time.perf_counter() - t
            run.metrics = {
                **result.metrics,
                "district_order": result.district_order,
                "hard_constraints": {"checked": list(HARD_CONSTRAINTS), "violations": 0},
                "timings_s": {k: round(v, 3) for k, v in {**timings, **result.solver["timings_s"]}.items()},
            }
            run.solver_parameters = result.solver
            run.input_snapshot_hash = result.input_hash
            run.status, run.completed_at = "completed", datetime.now(UTC)
            audit.record(s, Actor.system(), "planning.run.completed", "planning_run", run_id, "success",
                         {"cases": len(cases), "allocated": len(result.allocations), "unallocated": len(result.unallocated),
                          "input_snapshot_hash": result.input_hash, "solver_status": result.solver["status"]})  # fmt: skip
            s.commit()
            metrics = dict(run.metrics)
        if ctx:
            ctx.progress(processed=len(cases), succeeded=len(cases))
        return metrics
    except JobCancelled:
        _set_run_state(sessions, run_id, "cancelled", "cancelled", {})
        raise
    except PlanValidationError as exc:
        _set_run_state(sessions, run_id, "failed", short_error(exc), {"violations": [v.as_dict() for v in exc.violations[:20]]})
        raise
    except Exception as exc:
        log.exception("planning run failed", extra={"planning_run_id": str(run_id)})
        _set_run_state(sessions, run_id, "failed", short_error(exc), {"error": type(exc).__name__})
        raise


def _copy_rows(session: Session, model: type, rows: list[dict[str, Any]]) -> None:
    """Bulk-load `rows` with COPY inside the session's transaction (~20x faster than INSERT ... executemany over the
    Docker/Windows loopback: 100k allocations in seconds instead of a minute). Missing columns use their defaults."""
    if not rows:
        return
    table = model.__table__  # type: ignore[attr-defined]
    conn = session.connection()
    schema = (conn.get_execution_options().get("schema_translate_map") or {}).get(table.schema, table.schema)
    cols = [c for c in rows[0]]
    json_cols = {c.name for c in table.columns if isinstance(c.type, JSONB)}
    statement = f'COPY "{schema}"."{table.name}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) FROM STDIN'
    with conn.connection.driver_connection.cursor() as cur, cur.copy(statement) as copy:  # type: ignore[union-attr]
        for r in rows:
            copy.write_row([json.dumps(r[c]) if c in json_cols else r[c] for c in cols])


def _persist(session: Session, run: PlanningRun, cases: Sequence[PlannableCase], pools: Sequence[PoolSpec],
             slots: Sequence[SlotSpec], result) -> None:  # fmt: skip
    by_case = {c.case_id: c for c in cases}
    by_slot = {s.slot_id: s for s in slots}
    by_pool = {p.clinic_service_id: p for p in pools}
    travel = TravelModel(result.district_order)
    session.execute(delete(PlannedAllocation).where(PlannedAllocation.planning_run_id == run.id))
    session.execute(delete(UnallocatedCase).where(UnallocatedCase.planning_run_id == run.id))
    rows = []
    for a in result.allocations:
        c, s = by_case[a.case_id], by_slot[a.slot_id]
        p = by_pool[s.clinic_service_id]
        tier = travel.tier(c.district_code, p.district_code)
        rows.append({
            "id": uuid5_of(f"allocation:{run.id}:{c.case_id}"), "planning_run_id": run.id, "care_gap_case_id": c.case_id,
            "resident_id": c.resident_id, "clinic_id": p.clinic_id, "clinic_service_id": p.clinic_service_id,
            "capacity_slot_id": s.slot_id, "clinic_key": p.clinic_key, "service_key": p.service_key, "priority": c.priority or None,
            "district_code": c.district_code, "language": c.language, "proposed_start_time": s.start, "proposed_end_time": s.end,
            "allocation_status": "proposed", "synthetic_status": "simulated",
            "explanation": (f"Proposed at {p.clinic_key} (district {p.district_code}, travel tier {tier}) for {p.service_key}; "
                            f"priority {c.priority or 'unspecified'}; slot {s.start:%Y-%m-%d %H:%M}Z. Simulated plan, not a booking."),
        })  # fmt: skip
    _copy_rows(session, PlannedAllocation, rows)
    un_rows = []
    for u in result.unallocated:
        c = by_case[u.case_id]
        un_rows.append({
            "id": uuid5_of(f"unallocated:{run.id}:{c.case_id}"), "planning_run_id": run.id, "care_gap_case_id": c.case_id,
            "resident_id": c.resident_id, "service_key": c.service_key or None, "priority": c.priority or None,
            "district_code": c.district_code or None, "language": c.language, "reason_code": u.reason_code,
            "explanation": u.explanation, "details": u.details, "synthetic_status": "simulated",
        })  # fmt: skip
    _copy_rows(session, UnallocatedCase, un_rows)


# ------------------------------------------------------------------------------------------------ confirmation
def confirm_allocation(session: Session, allocation_id: uuid.UUID, actor: Actor) -> tuple[PlannedAllocation, bool]:
    """Re-validate the slot, then reserve one seat and mark the allocation confirmed. Idempotent for an already
    confirmed allocation (returns `(allocation, False)`). Does NOT create an appointment (later wave) and does not
    change the care-gap case status (the caller / care-gap module owns that transition). Caller commits."""
    alloc = session.get(PlannedAllocation, allocation_id, with_for_update=True)
    if alloc is None:
        raise NotFound("Allocation not found.")
    if alloc.allocation_status == "confirmed":
        return alloc, False

    def refuse(reason: str, field: str) -> Conflict:
        audit.record(session, actor, "planning.allocation.confirm", "planned_allocation", alloc.id, "failure", {"reason": reason})
        session.commit()  # keep the audit trail of the refused confirmation
        return Conflict(reason, [{"field": field, "reason": reason}])

    if alloc.allocation_status != "proposed":
        raise refuse(f"Allocation is {alloc.allocation_status} and cannot be confirmed.", "allocation_status")
    run = session.get(PlanningRun, alloc.planning_run_id)
    if run is None or run.status != "completed":
        raise refuse("The planning run is not completed.", "planning_run_id")
    slot = session.get(CapacitySlot, alloc.capacity_slot_id, with_for_update=True)
    clinic = session.get(Clinic, alloc.clinic_id)
    service = session.get(ClinicService, alloc.clinic_service_id)
    if slot is None or clinic is None or service is None:
        raise refuse("The proposed slot no longer exists.", "capacity_slot_id")
    if slot.slot_status != "open" or clinic.operating_status != "open" or not service.active:
        raise refuse("The slot, clinic or service is no longer open.", "capacity_slot_id")
    if slot.reserved_capacity >= slot.available_capacity:
        raise refuse("The slot has no remaining capacity.", "capacity_slot_id")

    slot.reserved_capacity += 1
    alloc.allocation_status, alloc.confirmed_at, alloc.confirmed_by = "confirmed", datetime.now(UTC), actor.id
    session.execute(  # other runs' proposals for the same case are now moot
        update(PlannedAllocation)
        .where(PlannedAllocation.care_gap_case_id == alloc.care_gap_case_id, PlannedAllocation.id != alloc.id,
               PlannedAllocation.allocation_status == "proposed")
        .values(allocation_status="superseded")
    )  # fmt: skip
    try:
        session.flush()
    except IntegrityError:  # partial unique index: the case already has a confirmed allocation elsewhere
        session.rollback()
        alloc = session.get(PlannedAllocation, allocation_id)
        assert alloc is not None
        raise refuse("The case already has a confirmed allocation.", "care_gap_case_id") from None
    audit.record(session, actor, "planning.allocation.confirm", "planned_allocation", alloc.id, "success",
                 {"capacity_slot_id": str(slot.id), "care_gap_case_id": str(alloc.care_gap_case_id), "planning_run_id": str(run.id)})  # fmt: skip
    return alloc, True


# ------------------------------------------------------------------------------------------------ analytics
def capacity_overview(session: Session, start: datetime | None, end: datetime | None, district_code: str | None) -> dict[str, Any]:
    latest = session.execute(
        select(PlanningRun).where(PlanningRun.status == "completed").order_by(PlanningRun.completed_at.desc()).limit(1)
    ).scalar_one_or_none()
    source = "query"
    if start is None and end is None and latest is not None:
        start, end, source = latest.planning_period_start, latest.planning_period_end, "latest_run"
    elif start is None and end is None:
        source = "all"

    def window(stmt, start_col, end_col):
        if start is not None:
            stmt = stmt.where(start_col >= start)
        if end is not None:
            stmt = stmt.where(end_col <= end)
        return stmt

    seats = (
        select(Clinic.district_code, func.coalesce(func.sum(CapacitySlot.available_capacity), 0),
               func.coalesce(func.sum(CapacitySlot.reserved_capacity), 0))
        .join(Clinic, Clinic.id == CapacitySlot.clinic_id)
        .where(CapacitySlot.slot_status == "open")
        .group_by(Clinic.district_code)
        .order_by(Clinic.district_code)
    )  # fmt: skip
    if district_code:
        seats = seats.where(Clinic.district_code == district_code)
    planned: dict[str, int] = {}
    if latest is not None:
        planned = dict(session.execute(
            select(Clinic.district_code, func.count()).select_from(PlannedAllocation)
            .join(Clinic, Clinic.id == PlannedAllocation.clinic_id)
            .where(PlannedAllocation.planning_run_id == latest.id, PlannedAllocation.allocation_status == "proposed")
            .group_by(Clinic.district_code)
        ).all())  # fmt: skip
    by_district = []
    for d, total, reserved in session.execute(window(seats, CapacitySlot.start_time, CapacitySlot.end_time)).all():
        by_district.append({"district_code": d, "total_seats": int(total), "reserved_seats": int(reserved),
                            "remaining_seats": int(total - reserved), "utilization": (reserved / total) if total else None,
                            "planned_seats": planned.get(d, 0)})  # fmt: skip
    total = sum(r["total_seats"] for r in by_district)
    reserved = sum(r["reserved_seats"] for r in by_district)
    m = latest.metrics if latest is not None else {}
    return {
        "simulated": True,
        "synthetic_status": "simulated",
        "window": {"start": start, "end": end, "source": source},
        "capacity": {"total_seats": total, "reserved_seats": reserved, "remaining_seats": total - reserved,
                     "utilization": (reserved / total) if total else None,
                     "note": "reserved = confirmed allocations; planned_seats are proposals from the latest run, not bookings"},  # fmt: skip
        "by_district": by_district,
        "latest_run": None if latest is None else {
            "id": latest.id, "completed_at": latest.completed_at, "planning_period_start": latest.planning_period_start,
            "planning_period_end": latest.planning_period_end, "cases_total": m.get("cases_total"), "allocated": m.get("allocated"),
            "unallocated": m.get("unallocated"), "coverage": m.get("coverage"), "plan_utilization": m.get("utilization"),
            "reason_counts": m.get("reason_counts", {}),
            "equity": {d: {k: v for k, v in (m.get("equity", {}).get(d) or {}).items() if k != "groups"} for d in ("district", "language")},
        },
    }  # fmt: skip
