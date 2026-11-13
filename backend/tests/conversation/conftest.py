# ruff: noqa: E501
"""Resident conversation fixtures: the engagement test world (residents, cases, a simulated clinic and slots) loaded by path,
the same way the Ask Anticipa tests do. DB `anticipa_test` only, every test rolled back."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from anticipa.conversation import service

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))  # lets tests `import conv_samples` (importlib import mode)


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def fresh():
    service.reset_state()
    yield
    service.reset_state()


@pytest.fixture(scope="session")
def world_module():
    return _load("anticipa_engagement_test_world", TESTS / "engagement" / "world.py")


@pytest.fixture
def world(db_session, world_module):
    return world_module.World(db_session)
