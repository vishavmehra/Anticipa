"""Planning test fixtures.

Pure-solver tests need nothing. Integration tests use the local docker Postgres database `anticipa_test` (never the dev
database, never a remote one) and isolate themselves in a throw-away schema through `schema_translate_map`, so they can
neither clash with other workstreams' tests nor touch their tables.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from anticipa.api.errors import install_exception_handlers
from anticipa.api.middleware import RequestContextMiddleware
from anticipa.config import ConfigError, get_settings
from anticipa.consent.models import ConsentRecord
from anticipa.core.auth import CurrentUser, get_current_user
from anticipa.core.models import AuditEvent
from anticipa.db import Base, make_engine, make_sessionmaker
from anticipa.jobs.models import ProcessingItem, ProcessingJob
from anticipa.planning import adapters, routes, service
from anticipa.planning.domain import PlanConfig, PlannableCase, PoolSpec, SlotSpec
from anticipa.planning.models import (
    CapacitySlot,
    Clinic,
    ClinicService,
    PlannedAllocation,
    PlanningRun,
    UnallocatedCase,
)

TEST_DB = "anticipa_test"
T0 = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
HORIZON = (datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 11, 17, tzinfo=UTC))
PLANNING_TABLES = [Clinic, ClinicService, CapacitySlot, PlanningRun, PlannedAllocation, UnallocatedCase]


def make_config(**overrides) -> PlanConfig:
    kw = {"horizon_start": HORIZON[0], "horizon_end": HORIZON[1], "time_limit_seconds": 20}
    return PlanConfig(**{**kw, **overrides})


def pool(key: str, district: str, service: str, minutes: int = 30, **kw) -> PoolSpec:
    return PoolSpec(
        uuid.uuid5(uuid.NAMESPACE_DNS, f"pool:{key}:{service}"), uuid.uuid5(uuid.NAMESPACE_DNS, f"clinic:{key}"),
        key, district, service, minutes, **kw,
    )  # fmt: skip


def slot(p: PoolSpec, hour: int, free: int, day: int = 0, minutes: int | None = None, **kw) -> SlotSpec:
    start = T0.replace(hour=hour) + timedelta(days=day)
    return SlotSpec(
        uuid.uuid5(uuid.NAMESPACE_DNS, f"slot:{p.clinic_service_id}:{start.isoformat()}"), p.clinic_service_id, start,
        start + timedelta(minutes=minutes or p.duration_minutes), free, **kw,
    )  # fmt: skip


def case(n: int, service: str, district: str, priority: str = "routine", language: str = "en",
         resident: int | None = None, consent_denied: bool = False) -> PlannableCase:  # fmt: skip
    r = n if resident is None else resident
    return PlannableCase(
        uuid.uuid5(uuid.NAMESPACE_URL, f"case:{n}"), uuid.uuid5(uuid.NAMESPACE_URL, f"resident:{r}"),
        service, priority, district, language, T0 + timedelta(minutes=n), consent_denied,
    )  # fmt: skip


def local_base_url():
    try:
        base = make_url(get_settings().sqlalchemy_url())
    except ConfigError as exc:
        pytest.skip(f"integration tests need a local database: {exc}")
    if base.host not in ("localhost", "127.0.0.1", "::1"):
        pytest.skip("integration tests only run against the local docker Postgres")
    return base


@pytest.fixture(scope="session")
def planning_schema() -> Iterator[str]:
    yield f"planning_it_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def planning_engine(planning_schema) -> Iterator:
    """Engine on `anticipa_test` whose `anticipa` schema is translated to a private throw-away schema."""
    base = local_base_url()
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            if not conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB}).first():
                conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"local Postgres unavailable: {exc}")
    finally:
        admin.dispose()
    root = make_engine(base.set(database=TEST_DB))
    assert root.url.database == TEST_DB  # guard: never create/drop anything elsewhere
    with root.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{planning_schema}"'))
    engine = root.execution_options(schema_translate_map={"anticipa": planning_schema})
    with engine.begin() as conn:
        tables = [AuditEvent.__table__, ProcessingJob.__table__, ProcessingItem.__table__, *(m.__table__ for m in PLANNING_TABLES)]
        Base.metadata.create_all(conn, tables=tables)
        adapters.care_gap_cases.create(conn)  # stand-ins for the care-gap / resident tables (projection columns only)
        adapters.residents.create(conn)
        Base.metadata.create_all(conn, tables=[ConsentRecord.__table__])  # FK to the stand-in residents table above
    yield engine
    with root.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{planning_schema}" CASCADE'))
    root.dispose()


@pytest.fixture
def sessions(planning_engine, planning_schema) -> sessionmaker:
    """Empty tables for every test."""
    names = [m.__tablename__ for m in PLANNING_TABLES] + ["audit_events", "processing_items", "processing_jobs", "care_gap_cases", "residents", "consent_records"]
    with planning_engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(f'"{planning_schema}"."{n}"' for n in names) + " CASCADE"))
    return make_sessionmaker(planning_engine)


@pytest.fixture
def f() -> SimpleNamespace:
    """Builders for hand-made planning instances (importlib test mode cannot import sibling test modules)."""
    return SimpleNamespace(config=make_config, pool=pool, slot=slot, case=case, T0=T0, HORIZON=HORIZON)


def _seed_world(sessions, n_cases=300, *, seed=5, seats=250, status="approved_for_planning"):
    """A small simulated network + residents + open care-gap cases. Returns (PlannableCases, NetworkParams)."""
    from anticipa.planning import adapters as ad
    from anticipa.planning.synthetic import NetworkParams, generate_cases, generate_network, persist_network

    params = NetworkParams(seed=seed, clinics_per_district=1, days=2, districts=("D01", "D02", "D03"), target_total_seats=seats)
    cases = generate_cases(n_cases, seed=seed, districts=params.districts)
    with sessions() as s:
        persist_network(s, generate_network(params))
        residents = {c.resident_id: {"id": c.resident_id, "district_code": c.district_code, "preferred_language": c.language}
                     for c in cases}  # fmt: skip
        s.execute(ad.residents.insert(), list(residents.values()))
        s.execute(ad.care_gap_cases.insert(), [{"id": c.case_id, "resident_id": c.resident_id, "care_activity": c.service_key,
                                                "status": status, "priority": c.priority, "opened_at": c.opened_at} for c in cases])  # fmt: skip
        s.commit()
    return cases, params


def _new_run(sessions, **overrides):
    from anticipa.planning import service as svc
    from anticipa.planning.adapters import CaseFilters

    cfg = make_config(**overrides)
    with sessions() as s:
        run = svc.create_run(s, run_id=uuid.uuid4(), config=cfg, case_filters=CaseFilters(), created_by=None, job_id=None)
        s.commit()
        return run.id


@pytest.fixture
def seed_world():
    return _seed_world


@pytest.fixture
def new_run():
    return _new_run


NOW = datetime(2026, 11, 9, tzinfo=UTC)  # before every slot the tests plan (planning horizon starts 2026-11-10)


@pytest.fixture
def hold_calls(monkeypatch) -> list[dict]:
    """Pins the planning clock and replaces the engagement appointment hook with a recorder (its tables are not part
    of the private test schema). Tests that care read the recorded calls; the hook returns `created=True`."""
    calls: list[dict] = []
    monkeypatch.setattr(service, "_utcnow", lambda: NOW)

    def fake(session, allocation_id, *, actor, user=None, source="manual", job_id=None):
        alloc = session.get(PlannedAllocation, allocation_id)
        calls.append({"allocation_id": allocation_id, "actor": actor, "user": user, "source": source, "status_seen": alloc.allocation_status,
                      "session": session})  # fmt: skip
        return object(), True

    try:
        import anticipa.engagement.service as engagement

        monkeypatch.setattr(engagement, "on_allocation_confirmed", fake)
    except ImportError:
        pass
    return calls


@pytest.fixture(autouse=True)
def _stubs(hold_calls):
    return hold_calls


@pytest.fixture
def client(sessions):
    app = FastAPI()
    app.state.sessionmaker = sessions
    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(routes.router, prefix="/api/v1")

    def fake_user(request: Request) -> CurrentUser:
        role = request.headers.get("x-test-role", "population_admin")
        return CurrentUser(uuid.uuid5(uuid.NAMESPACE_DNS, role), f"{role}@example.test", role)

    app.dependency_overrides[get_current_user] = fake_user
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


