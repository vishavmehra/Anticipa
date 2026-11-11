"""Closure fixtures (deliver a working, tested, demonstrable Anticipa MVP): reuse the engagement test world (loaded by
path). DB fixtures come from tests/conftest.py (DB `anticipa_test` only)."""

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
    return load_by_path("anticipa_engagement_test_world", TESTS / "engagement" / "world.py")


@pytest.fixture
def world(db_session, world_module):
    return world_module.World(db_session)
