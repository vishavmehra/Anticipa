"""Shared fixtures for Model Router tests. anyio (installed) runs the async tests on asyncio."""

from __future__ import annotations

import pytest

from anticipa.ai.config import RouterConfig, load_config
from anticipa.ai.testing import make_test_router


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def cfg() -> RouterConfig:
    return load_config()


@pytest.fixture
def make_router(cfg):
    def _make(adapters: dict, *, cache=None, sink=None):
        return make_test_router(adapters, config=cfg, cache=cache, sink=sink)

    return _make
