"""Engagement fixtures (deliver a working, tested, demonstrable Anticipa MVP): `world` builds synthetic residents,
care-gap cases, a simulated clinic and planned allocations in the rolled-back test transaction (DB `anticipa_test`)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]


def load_by_path(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def world_module():
    return load_by_path("anticipa_engagement_test_world", Path(__file__).with_name("world.py"))


@pytest.fixture
def world(db_session, world_module):
    return world_module.World(db_session)
