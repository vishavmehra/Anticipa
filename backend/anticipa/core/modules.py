"""Optional per-module extension points, so parallel workstreams plug in without editing foundation code.

Conventions (each file is optional):
  anticipa/<module>/routes.py    exposes `router: APIRouter` (own prefix, e.g. "/residents"); mounted under
  /api/v1
  anticipa/<module>/handlers.py  registers background jobs with @register_job; imported by the worker and API
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType

log = logging.getLogger(__name__)

MODULES = (
    "datafabric", "datafabric.docgen", "ai", "evidence", "guidelines", "caregaps", "planning",
    "engagement", "closure", "analytics", "evaluation",
)  # fmt: skip


def optional_import(name: str) -> ModuleType | None:
    """Import `name` if it exists. A ModuleNotFoundError *inside* an existing module is re-raised."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name or (exc.name and name.startswith(exc.name + ".")):
            return None
        raise


def load_module_files(filename: str) -> list[ModuleType]:
    loaded = []
    for module in MODULES:
        mod = optional_import(f"anticipa.{module}.{filename}")
        if mod is not None:
            log.info("loaded %s.%s", module, filename)
            loaded.append(mod)
    return loaded
