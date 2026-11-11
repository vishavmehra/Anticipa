"""Guideline-engine fixtures. DB fixtures (`db_session`, `api_client`, `auth_headers`) come from tests/conftest.py and
run against DB `anticipa_test` only. `seed` builds clearly synthetic residents/records/rules (see seed.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SEED_PATH = Path(__file__).with_name("seed.py")


def load_seed():
    name = "anticipa_guideline_test_seed"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SEED_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def seed_module():
    return load_seed()


@pytest.fixture
def seed(db_session, seed_module):
    return seed_module.Seeder(db_session)
