"""Job state machine: submit, claim, finish, retry, cancel, stale requeue. All transitions are guarded in SQL.

States: pending | queued | running | partially_completed | completed | failed | cancelled.
`submit_job` creates `queued` jobs (`pending` is reserved for jobs created before they are runnable).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from anticipa.core.errors import Conflict, InvalidTransition, NotFound, ValidationFailed
from anticipa.jobs.context import short_error
from anticipa.jobs.models import TERMINAL_STATUSES, ProcessingJob
from anticipa.jobs.registry import JOB_TYPE_RE

RETRYABLE_STATUSES = frozenset({"failed", "partially_completed", "cancelled"})


@dataclass(frozen=True)
class SubmitResult:
    job: ProcessingJob
    # False => an existing job with the same idempotency_key was returned (respond HTTP 200, not 202)
    created: bool


def submit_job(
    session: Session,
    job_type: str,
    input_scope: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    created_by: uuid.UUID | None = None,
    *,
    config_version: str | None = None,
    max_retries: int = 3,
) -> SubmitResult:
    """Queue a job. With an existing `idempotency_key` the existing job is returned (contract 17 §4).

    Flushes but does not commit: the caller commits (so it can add its audit event in the same transaction).
    """
    if not JOB_TYPE_RE.match(job_type):
        raise ValidationFailed(f"job_type must be namespaced like 'module.name': {job_type!r}")
    values = {
        "id": uuid.uuid4(),
        "job_type": job_type,
        "status": "queued",
        "input_scope": input_scope or {},
        "config_version": config_version,
        "idempotency_key": idempotency_key,
        "created_by": created_by,
        "max_retries": max_retries,
    }
    stmt = pg_insert(ProcessingJob).values(**values)
    if idempotency_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=["idempotency_key"])
    inserted = session.execute(stmt.returning(ProcessingJob.id)).scalar_one_or_none()
    if inserted is not None:
        job = session.get(ProcessingJob, inserted, populate_existing=True)
        assert job is not None
        return SubmitResult(job, created=True)
    existing = session.execute(
        select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key)
    ).scalar_one()
    if existing.job_type != job_type:
        raise Conflict("idempotency_key is already used by a different job type")
    return SubmitResult(existing, created=False)


def get_job(session: Session, job_id: uuid.UUID, *, for_update: bool = False) -> ProcessingJob:
    job = session.get(ProcessingJob, job_id, with_for_update=for_update, populate_existing=for_update)
    if job is None:
        raise NotFound("Job not found.")
    return job


def cancel_job(session: Session, job_id: uuid.UUID) -> tuple[ProcessingJob, str]:
    """Queued/pending jobs cancel immediately; running jobs get `cancel_requested` and stop cooperatively."""
    job = get_job(session, job_id, for_update=True)
    previous = job.status
    if previous in TERMINAL_STATUSES:
        raise InvalidTransition(f"A {previous} job cannot be cancelled.")
    if previous == "running":
        job.cancel_requested = True
    else:
        job.status = "cancelled"
        job.cancel_requested = True
        job.completed_at = func.now()
        job.locked_by = None
    session.flush()
    session.refresh(job)
    return job, previous


def retry_job(session: Session, job_id: uuid.UUID) -> tuple[ProcessingJob, str]:
    """Manually re-queue a failed/partially_completed/cancelled job. Counters, checkpoint and item rows are
    kept
    so the handler resumes; the automatic retry budget is reset."""
    job = get_job(session, job_id, for_update=True)
    previous = job.status
    if previous not in RETRYABLE_STATUSES:
        raise InvalidTransition(f"A {previous} job cannot be retried.")
    job.status = "queued"
    job.retry_count = 0
    job.error_summary = None
    job.cancel_requested = False
    job.completed_at = None
    job.next_run_at = None
    job.locked_by = None
    job.heartbeat_at = None
    session.flush()
    session.refresh(job)
    return job, previous


# ---------------------------------------------------------------- worker-side transitions


def backoff_delay(retry_count: int, base: float, cap: float) -> timedelta:
    return timedelta(seconds=min(cap, base * (2 ** max(retry_count - 1, 0))))


def claim_next(session: Session, worker_id: str) -> ProcessingJob | None:
    """Claim the oldest runnable job (`FOR UPDATE SKIP LOCKED`) and mark it running. Commits."""
    job = session.execute(
        select(ProcessingJob)
        .where(
            ProcessingJob.status == "queued",
            or_(ProcessingJob.next_run_at.is_(None), ProcessingJob.next_run_at <= func.now()),
        )
        .order_by(ProcessingJob.created_at, ProcessingJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).scalar_one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.locked_by = worker_id
    job.heartbeat_at = func.now()
    job.started_at = job.started_at or func.now()
    job.next_run_at = None
    session.commit()
    session.refresh(job)
    session.expunge(job)  # detached snapshot handed to the JobContext
    return job


def _owned(session: Session, job_id: uuid.UUID, worker_id: str) -> ProcessingJob | None:
    job = session.get(ProcessingJob, job_id, with_for_update=True, populate_existing=True)
    if job is None or job.status != "running" or job.locked_by != worker_id:
        return None
    return job


def _release(job: ProcessingJob) -> None:
    job.locked_by = None
    job.heartbeat_at = None


def finish_success(session: Session, job_id: uuid.UUID, worker_id: str) -> str | None:
    """Handler returned normally. Failed items => `partially_completed`, never `completed`."""
    job = _owned(session, job_id, worker_id)
    if job is None:
        return None
    job.status = "partially_completed" if job.failed_items > 0 else "completed"
    job.completed_at = func.now()
    _release(job)
    session.commit()
    return job.status


def finish_cancelled(session: Session, job_id: uuid.UUID, worker_id: str) -> str | None:
    job = _owned(session, job_id, worker_id)
    if job is None:
        return None
    job.status = "cancelled"
    job.completed_at = func.now()
    _release(job)
    session.commit()
    return job.status


def finish_failure(
    session: Session,
    job_id: uuid.UUID,
    worker_id: str,
    error: BaseException | str,
    *,
    permanent: bool = False,
    backoff_base: float = 5.0,
    backoff_cap: float = 300.0,
) -> str | None:
    """Handler failed. Requeue with exponential backoff until `max_retries` is exhausted, then `failed`."""
    job = _owned(session, job_id, worker_id)
    if job is None:
        return None
    _apply_failure(job, short_error(error), permanent, backoff_base, backoff_cap)
    session.commit()
    return job.status


def _apply_failure(job: ProcessingJob, summary: str, permanent: bool, base: float, cap: float) -> None:
    job.retry_count += 1
    job.error_summary = summary
    if permanent or job.retry_count > job.max_retries:
        job.status = "failed"
        job.completed_at = func.now()
        job.next_run_at = None
    else:
        job.status = "queued"
        job.next_run_at = func.now() + backoff_delay(job.retry_count, base, cap)
    _release(job)


def requeue_stale(
    session: Session, stale_after_seconds: float, *, backoff_base: float = 5.0, backoff_cap: float = 300.0
) -> list[uuid.UUID]:
    """Recover jobs whose worker stopped heartbeating: count it as a failed attempt (or cancel if
    requested)."""
    jobs = (
        session.execute(
            select(ProcessingJob)
            .where(
                ProcessingJob.status == "running",
                ProcessingJob.heartbeat_at < func.now() - timedelta(seconds=stale_after_seconds),
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for job in jobs:
        if job.cancel_requested:
            job.status = "cancelled"
            job.completed_at = func.now()
            _release(job)
        else:
            _apply_failure(job, "Worker lost: heartbeat went stale.", False, backoff_base, backoff_cap)
    session.commit()
    return [j.id for j in jobs]
