"""Imports every module's table definitions so `Base.metadata` is complete (Alembic, tests, schema checks)."""

from anticipa.ai import models as ai_models
from anticipa.core import models as core_models
from anticipa.core.modules import MODULES, optional_import
from anticipa.datafabric import models as datafabric_models
from anticipa.db import Base
from anticipa.evidence import models as evidence_models
from anticipa.jobs import models as jobs_models

# Every other module registers its tables like its routes/handlers: `anticipa/<module>/models.py`.
for _module in MODULES:
    optional_import(f"anticipa.{_module}.models")

__all__ = ["Base", "ai_models", "core_models", "datafabric_models", "evidence_models", "jobs_models"]
