"""Job handler registry: `@register_job("<module>.<name>")` on `def handle(ctx: JobContext) -> None`."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anticipa.jobs.context import JobContext

JobHandler = Callable[["JobContext"], None]

JOB_TYPE_RE = re.compile(
    r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
)  # namespaced by module, e.g. datafabric.ingest_batch
_HANDLERS: dict[str, JobHandler] = {}


def register_job(job_type: str) -> Callable[[JobHandler], JobHandler]:
    if not JOB_TYPE_RE.match(job_type):
        raise ValueError(f"job_type must be namespaced like 'module.name': {job_type!r}")

    def decorator(fn: JobHandler) -> JobHandler:
        existing = _HANDLERS.get(job_type)
        if existing is not None and (existing.__module__, existing.__qualname__) != (
            fn.__module__,
            fn.__qualname__,
        ):
            raise ValueError(f"job_type already registered by {existing.__module__}: {job_type!r}")
        _HANDLERS[job_type] = fn
        return fn

    return decorator


def get_handler(job_type: str) -> JobHandler | None:
    return _HANDLERS.get(job_type)


def registered_job_types() -> list[str]:
    return sorted(_HANDLERS)
