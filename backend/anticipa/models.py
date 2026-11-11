"""Imports every module's table definitions so `Base.metadata` is complete (Alembic autogenerate, tests)."""

from anticipa.ai import models as ai_models
from anticipa.core import models as core_models
from anticipa.datafabric import models as datafabric_models
from anticipa.db import Base
from anticipa.evidence import models as evidence_models
from anticipa.jobs import models as jobs_models

# Later waves: import their `models` modules here (guidelines, caregaps, planning, engagement, closure,
# analytics).
__all__ = ["Base", "ai_models", "core_models", "datafabric_models", "evidence_models", "jobs_models"]
