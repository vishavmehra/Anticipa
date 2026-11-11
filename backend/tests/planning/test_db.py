"""Integration tests on `anticipa_test` (private schema): migration 0003, persistence, run execution, job handler."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, inspect, select, text

from anticipa.core.models import AuditEvent
from anticipa.db import Base
from anticipa.jobs.context import JobContext
from anticipa.jobs.models import ProcessingJob
from anticipa.planning import service
from anticipa.planning.adapters import CaseFilters, CaseSourceUnavailable, load_plannable_cases
from anticipa.planning.domain import PlanConfig
from anticipa.planning.handlers import generate_capacity, run_planning
from anticipa.planning.models import (
    CapacitySlot,
    Clinic,
    ClinicService,
    PlannedAllocation,
    PlanningRun,
    UnallocatedCase,
)
from anticipa.planning.synthetic import NetworkParams, generate_network, persist_network
from anticipa.planning.validate import validate_plan

pytestmark = pytest.mark.integration
MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0003_planning.py"
PLANNING_TABLES = ("clinics", "clinic_services", "capacity_slots", "planning_runs", "planned_allocations", "unallocated_cases")


# ------------------------------------------------------------------------------------------------ migration
def test_migration_0003_matches_models_and_locks_down_tables(planning_engine):
    spec = importlib.util.spec_from_file_location("migration_0003", MIGRATION)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert (mig.revision, mig.down_revision) == ("0003", "0002") and set(mig.TABLES) == set(PLANNING_TABLES)
    schema = f"planning_mig_{uuid.uuid4().hex[:8]}"
    mig.SCHEMA = schema
    root = planning_engine.execution_options(schema_translate_map={})  # real schema names from here on
    with root.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'CREATE TABLE "{schema}".processing_jobs (id uuid PRIMARY KEY)'))  # stand-in for the 0001 table
    try:
        with root.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                mig.upgrade()
        insp = inspect(root)
        assert set(PLANNING_TABLES) <= set(insp.get_table_names(schema=schema))
        for name in PLANNING_TABLES:  # no drift between the hand-written migration and the ORM models
            migrated = {(c["name"], c["nullable"]) for c in insp.get_columns(name, schema=schema)}
            modelled = {(c.name, c.nullable) for c in Base.metadata.tables[f"anticipa.{name}"].columns}
            assert migrated == modelled, name
            assert {i["name"] for i in insp.get_indexes(name, schema=schema)} >= {
                i.name for i in Base.metadata.tables[f"anticipa.{name}"].indexes
            }
            assert {c["name"] for c in insp.get_check_constraints(name, schema=schema)} == {
                c.name for c in Base.metadata.tables[f"anticipa.{name}"].constraints if c.__class__.__name__ == "CheckConstraint"
            }, name
        with root.connect() as conn:
            rls = dict(conn.execute(text("SELECT relname, relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                         "WHERE n.nspname = :s AND relkind = 'r'"), {"s": schema}).all())
        assert all(rls[t] for t in PLANNING_TABLES)
        with root.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                mig.downgrade()
        assert not set(PLANNING_TABLES) & set(inspect(root).get_table_names(schema=schema))
    finally:
        with root.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


# ------------------------------------------------------------------------------------------------ generator
def test_capacity_generator_is_deterministic_idempotent_and_labelled_simulated(sessions):
    params = NetworkParams(seed=9, clinics_per_district=2, days=2, districts=("D01", "D02"))
    a, b = generate_network(params), generate_network(params)
    assert a.slots == b.slots and a.clinics == b.clinics and a.services == b.services
    assert generate_network(NetworkParams(seed=10, clinics_per_district=2, days=2, districts=("D01", "D02"))).slots != a.slots
    with sessions() as s:
        first = persist_network(s, a)
        s.commit()
    with sessions() as s:
        assert persist_network(s, b) == {"clinics": 0, "clinic_services": 0, "capacity_slots": 0}  # idempotent
        assert first["capacity_slots"] == len(a.slots) > 0
        assert not s.execute(select(CapacitySlot).where(CapacitySlot.synthetic_status != "simulated")).first()
        assert not s.execute(select(Clinic).where(Clinic.synthetic_status != "simulated")).first()


def test_generate_capacity_job_handler(sessions):
    with sessions() as s:
        job = ProcessingJob(id=uuid.uuid4(), job_type="planning.generate_capacity", status="running", locked_by="w1",
                            input_scope={"seed": 3, "days": 1, "clinics_per_district": 1, "districts": ["D01"]})  # fmt: skip
        s.add(job)
        s.commit()
        s.refresh(job)
    generate_capacity(JobContext(sessions, job, "w1"))
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(CapacitySlot)) > 0
        assert s.scalar(select(AuditEvent.id).where(AuditEvent.action == "planning.capacity.generate")) is not None


# ------------------------------------------------------------------------------------------------ adapter
def test_adapter_filters_and_maps_cases(sessions, seed_world):
    cases, _ = seed_world(sessions, 60)
    with sessions() as s:
        loaded = load_plannable_cases(s)
        assert {c.case_id for c in loaded} == {c.case_id for c in cases}
        d1 = load_plannable_cases(s, {"district_codes": ["D01"], "limit": 5})
        assert 0 < len(d1) <= 5 and all(c.district_code == "D01" for c in d1)
        assert load_plannable_cases(s, {"status": ["awaiting_capacity"]}) == []
    with pytest.raises(Exception):  # noqa: B017,PT011  (pydantic: only plannable statuses are accepted)
        CaseFilters(status=["completed"])


def test_adapter_reports_a_missing_case_table_clearly(sessions, planning_engine, planning_schema):
    with planning_engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{planning_schema}".care_gap_cases RENAME TO care_gap_cases_x'))
    try:
        with sessions() as s, pytest.raises(CaseSourceUnavailable):
            load_plannable_cases(s)
    finally:
        with planning_engine.begin() as conn:
            conn.execute(text(f'ALTER TABLE "{planning_schema}".care_gap_cases_x RENAME TO care_gap_cases'))


# ------------------------------------------------------------------------------------------------ run execution
def test_execute_run_persists_a_valid_explained_plan(sessions, seed_world, new_run):
    cases, _ = seed_world(sessions, 400, seats=250)
    run_id = new_run(sessions)
    metrics = service.execute_run(sessions, run_id)
    with sessions() as s:
        run = s.get(PlanningRun, run_id)
        allocs = s.scalars(select(PlannedAllocation).where(PlannedAllocation.planning_run_id == run_id)).all()
        un = s.scalars(select(UnallocatedCase).where(UnallocatedCase.planning_run_id == run_id)).all()
        assert run.status == "completed" and run.started_at and run.completed_at and run.input_snapshot_hash
        assert run.solver_parameters["status"] in ("OPTIMAL", "FEASIBLE") and run.solver_parameters["seed"] == run.seed == 42
        assert run.metrics["hard_constraints"]["violations"] == 0 and "equity" in run.metrics and run.synthetic_status == "simulated"
        assert len(allocs) == metrics["allocated"] > 0 and len(un) == metrics["unallocated"] > 0
        assert len(allocs) + len(un) == len(cases)
        assert all(a.allocation_status == "proposed" and a.explanation and a.synthetic_status == "simulated" for a in allocs)
        assert all(u.explanation and u.reason_code for u in un)
        # re-derive the constraints from the persisted rows only (09 §evaluation): capacity per slot + service match
        pools, slots = service.load_capacity(s, run.planning_period_start, run.planning_period_end)
        by_case = {c.case_id: c for c in cases}
        from anticipa.planning.domain import Allocation

        assert validate_plan([Allocation(a.care_gap_case_id, a.capacity_slot_id) for a in allocs], cases, pools, slots,
                             PlanConfig.model_validate(run.config)) == []  # fmt: skip
        assert all(by_case[a.care_gap_case_id].service_key == a.service_key for a in allocs)
        assert s.scalar(select(AuditEvent.id).where(AuditEvent.action == "planning.run.completed")) is not None
    assert service.execute_run(sessions, run_id) == metrics  # completed runs are idempotent


def test_run_is_reproducible_from_its_recorded_config(sessions, seed_world, new_run):
    seed_world(sessions, 200, seats=150)
    a, b = new_run(sessions, seed=7), new_run(sessions, seed=7)
    service.execute_run(sessions, a)
    service.execute_run(sessions, b)
    with sessions() as s:
        ra, rb = s.get(PlanningRun, a), s.get(PlanningRun, b)
        assert ra.input_snapshot_hash == rb.input_snapshot_hash
        pairs = lambda r: sorted((x.care_gap_case_id, x.capacity_slot_id) for x in s.scalars(  # noqa: E731
            select(PlannedAllocation).where(PlannedAllocation.planning_run_id == r)))  # fmt: skip
        assert pairs(a) == pairs(b)


def test_violating_plan_is_never_persisted(sessions, monkeypatch, seed_world, new_run):
    seed_world(sessions, 50, seats=40)
    run_id = new_run(sessions)
    from anticipa.planning.validate import Violation

    monkeypatch.setattr(service, "validate_plan", lambda *a, **k: [Violation("slot_capacity", None, None, "forced")])
    with pytest.raises(service.PlanValidationError):
        service.execute_run(sessions, run_id)
    with sessions() as s:
        run = s.get(PlanningRun, run_id)
        assert run.status == "failed" and "slot_capacity" in run.error_summary
        assert s.scalar(select(func.count()).select_from(PlannedAllocation)) == 0
        assert s.scalar(select(AuditEvent.outcome).where(AuditEvent.action == "planning.run.failed")) == "failure"


def test_run_fails_clearly_when_the_case_source_is_not_wired(sessions, planning_engine, planning_schema, new_run):
    run_id = new_run(sessions)
    with planning_engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{planning_schema}".care_gap_cases RENAME TO care_gap_cases_x'))
    try:
        with pytest.raises(CaseSourceUnavailable):
            service.execute_run(sessions, run_id)
    finally:
        with planning_engine.begin() as conn:
            conn.execute(text(f'ALTER TABLE "{planning_schema}".care_gap_cases_x RENAME TO care_gap_cases'))
    with sessions() as s:
        assert s.get(PlanningRun, run_id).status == "failed"


def test_planning_run_job_handler(sessions, seed_world, new_run):
    seed_world(sessions, 80, seats=60)
    run_id = new_run(sessions)
    with sessions() as s:
        job = ProcessingJob(id=uuid.uuid4(), job_type="planning.run", status="running", locked_by="w1",
                            input_scope={"planning_run_id": str(run_id)})  # fmt: skip
        s.add(job)
        s.commit()
        s.refresh(job)
    run_planning(JobContext(sessions, job, "w1"))
    with sessions() as s:
        assert s.get(PlanningRun, run_id).status == "completed"
        done = s.get(ProcessingJob, job.id)
        assert done.total_items == 80 and done.processed_items == 80


def test_confirmed_cases_are_not_planned_again(sessions, seed_world, new_run):
    seed_world(sessions, 120, seats=200)
    first = new_run(sessions)
    service.execute_run(sessions, first)
    with sessions() as s:
        alloc = s.scalars(select(PlannedAllocation).where(PlannedAllocation.planning_run_id == first)).first()
        service.confirm_allocation(s, alloc.id, service.Actor.system())
        s.commit()
        confirmed_case = alloc.care_gap_case_id
        assert confirmed_case not in {c.case_id for c in load_plannable_cases(s)}
    second = new_run(sessions)
    service.execute_run(sessions, second)
    with sessions() as s:
        assert not s.scalar(select(PlannedAllocation.id).where(PlannedAllocation.planning_run_id == second,
                                                               PlannedAllocation.care_gap_case_id == confirmed_case))  # fmt: skip
        assert s.scalar(select(func.count()).select_from(ClinicService)) > 0
