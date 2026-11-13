"""Fixtures for the evaluation tests (deliver a working, tested, demonstrable Anticipa MVP including the evaluation
report). Unit tests need nothing; `integration` tests use `anticipa_test` through the shared tests/conftest.py fixtures
(`db_session`, `api_client`, `auth_headers`) and never touch the dev database."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # lets tests `import builders` (importlib import mode)
