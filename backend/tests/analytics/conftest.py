"""Analytics fixtures (Backend C): reuse the engagement `world` (residents, cases, clinic, allocations) and the guideline
`seed` helpers (real approved versions and evaluations), both loaded by path. DB `anticipa_test` only; every test is
rolled back. The in-process TTL cache is bypassed (TTL 0) so each request recomputes against the test's own data."""
# ruff: noqa: E501

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from anticipa.ai.settings import AiSettings
from anticipa.analytics import geography, service, snapshot
from anticipa.caregaps.models import CaseStatusHistory
from anticipa.datafabric import synthea
from anticipa.datafabric.models import Resident

TESTS = Path(__file__).resolve().parents[1]


def load_by_path(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    for ttl in ("COMMAND_CENTER_TTL_S", "FUNNEL_TTL_S", "PROCESSING_TTL_S", "ALERTS_TTL_S"):
        monkeypatch.setattr(service, ttl, 0.0)
    monkeypatch.setattr(geography, "GEOGRAPHY_TTL_S", 0.0)
    monkeypatch.setattr(snapshot, "AUTO_REFRESH", False)  # a GET must not start a background refresh in tests
    service.clear_cache()
    yield
    service.clear_cache()


@pytest.fixture(autouse=True)
def isolated_inputs(monkeypatch, tmp_path):
    """No dependence on the developer's real manifests or .env: an empty Synthea dir and fixed AI settings."""
    monkeypatch.setattr(synthea, "SYNTHEA_DIR", tmp_path / "synthea")
    monkeypatch.setattr(
        service,
        "get_ai_settings",
        lambda: AiSettings(
            _env_file=None, anticipa_ai_run_budget_usd=Decimal("40"), anticipa_ai_daily_budget_usd=Decimal("5"),
            anticipa_nebius_credit_usd=Decimal("50"),
        ),
    )  # fmt: skip


@pytest.fixture(scope="session")
def world_module():
    return load_by_path("anticipa_engagement_test_world", TESTS / "engagement" / "world.py")


@pytest.fixture(scope="session")
def seed_module():
    return load_by_path("anticipa_guideline_test_seed", TESTS / "guidelines" / "seed.py")


@pytest.fixture
def world(db_session, world_module):
    return world_module.World(db_session)


@pytest.fixture
def seed(db_session, seed_module):
    return seed_module.Seeder(db_session)


@pytest.fixture
def sessions(db_session):
    """New sessions that join the test's rolled-back transaction (what the refresh, worker and CLI open)."""
    return sessionmaker(bind=db_session.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False)


@pytest.fixture
def make_case(world, db_session):
    """A case in `status` for a resident in `district`, with an append-only history that walks `path` (default: just
    `status`), all stamped `at` (default now). FK triggers are off for the test (see engagement/world.py)."""

    def make(status="planned", *, district="D1", priority="routine", path=None, at=None, **kw):
        case = world.case(status, **kw)
        db_session.get(Resident, case.resident_id).district_code = district
        case.priority = priority
        prev, base = None, at or datetime.now(UTC)
        for i, step in enumerate(path or [status]):  # strictly increasing stamps: the Windows clock ticks every ~15 ms
            db_session.add(
                CaseStatusHistory(case_id=case.id, from_status=prev, to_status=step, occurred_at=base + timedelta(milliseconds=i))
            )
            prev = step
        db_session.flush()
        return case

    return make
