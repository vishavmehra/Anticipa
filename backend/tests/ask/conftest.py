"""Ask Anticipa fixtures: the engagement test world (residents, cases) loaded by path, like the analytics tests. DB
`anticipa_test` only, every test rolled back. The analytics TTL cache is bypassed so each call recomputes."""
# ruff: noqa: E501

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from anticipa.analytics import service as analytics
from anticipa.analytics import snapshot
from anticipa.ask import service as ask_service
from anticipa.ask import tools
from anticipa.caregaps.models import CaseStatusHistory
from anticipa.datafabric.models import Resident

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))  # lets tests `import ask_samples` (importlib import mode)


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(analytics, "COMMAND_CENTER_TTL_S", 0.0)
    monkeypatch.setattr(analytics, "FUNNEL_TTL_S", 0.0)
    monkeypatch.setattr(tools, "CACHE_TTL_S", 0.0)
    monkeypatch.setattr(snapshot, "AUTO_REFRESH", False)
    analytics.clear_cache()
    ask_service.reset_state()
    yield
    analytics.clear_cache()
    ask_service.reset_state()


@pytest.fixture(scope="session")
def world_module():
    return _load("anticipa_engagement_test_world", TESTS / "engagement" / "world.py")


@pytest.fixture
def world(db_session, world_module):
    return world_module.World(db_session)


@pytest.fixture
def make_case(world, db_session):
    """A case in `status` for a resident in `district` (language `language`), with a status history row."""

    def make(status="planned", *, district="D1", language="en", programme=None, priority="routine", at=None):
        case = world.case(status, language=language, **({"activity": programme} if programme else {}))
        db_session.get(Resident, case.resident_id).district_code = district
        case.priority = priority
        db_session.add(CaseStatusHistory(case_id=case.id, from_status=None, to_status=status, occurred_at=at or datetime.now(UTC) - timedelta(minutes=1)))
        db_session.flush()
        return case

    return make
