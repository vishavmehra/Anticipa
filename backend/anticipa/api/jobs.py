"""Background job APIs (05 §17): list, get, progress, retry, cancel. Submission is internal
(`jobs.service.submit_job`)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from anticipa.api.deps import PageDep, SessionDep
from anticipa.api.envelope import Envelope, PagedEnvelope, ok, paged, paginate
from anticipa.core import audit
from anticipa.core.auth import CurrentUser, require_permission
from anticipa.core.rbac import JOBS_VIEW, PROCESSING_EXECUTE
from anticipa.jobs import service
from anticipa.jobs.models import JOB_STATUSES, ProcessingItem, ProcessingJob

router = APIRouter(prefix="/jobs", tags=["jobs"])
ViewUser = Annotated[CurrentUser, Depends(require_permission(JOBS_VIEW))]
ExecuteUser = Annotated[CurrentUser, Depends(require_permission(PROCESSING_EXECUTE))]


class JobOut(BaseModel):
    id: uuid.UUID
    job_type: str
    status: str
    config_version: str | None
    total_items: int | None
    processed_items: int
    succeeded_items: int
    failed_items: int
    retry_count: int
    max_retries: int
    error_summary: str | None
    cancel_requested: bool
    created_by: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None
    checkpoint: dict[str, Any] | None = None  # only for users allowed to execute processing


class JobProgress(BaseModel):
    job_id: uuid.UUID
    status: str
    total_items: int | None
    processed_items: int
    succeeded_items: int
    failed_items: int
    percent_complete: float | None
    item_status_counts: dict[str, int]
    heartbeat_at: datetime | None
    updated_at: datetime


def to_out(job: ProcessingJob, user: CurrentUser) -> JobOut:
    out = JobOut.model_validate(job, from_attributes=True)
    out.checkpoint = dict(job.checkpoint) if user.can(PROCESSING_EXECUTE) else None
    return out


@router.get("", response_model=PagedEnvelope[JobOut], summary="List jobs (newest first)")
def list_jobs(
    request: Request,
    session: SessionDep,
    user: ViewUser,
    params: PageDep,
    status: Annotated[str | None, Query(pattern="^(" + "|".join(JOB_STATUSES) + ")$")] = None,
    job_type: Annotated[str | None, Query(max_length=100)] = None,
):
    stmt = select(ProcessingJob).order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
    if status:
        stmt = stmt.where(ProcessingJob.status == status)
    if job_type:
        stmt = stmt.where(ProcessingJob.job_type == job_type)
    rows, total = paginate(session, stmt, params)
    return paged(request, [to_out(j, user) for j in rows], params, total)


@router.get("/{job_id}", response_model=Envelope[JobOut], summary="Get a job")
def get_job(job_id: uuid.UUID, request: Request, session: SessionDep, user: ViewUser):
    return ok(request, to_out(service.get_job(session, job_id), user))


@router.get("/{job_id}/progress", response_model=Envelope[JobProgress], summary="Job progress")
def get_progress(job_id: uuid.UUID, request: Request, session: SessionDep, user: ViewUser):
    job = service.get_job(session, job_id)
    counts = dict(
        session.execute(
            select(ProcessingItem.status, func.count())
            .where(ProcessingItem.job_id == job_id)
            .group_by(ProcessingItem.status)
        ).all()
    )
    percent = round(100 * job.processed_items / job.total_items, 1) if job.total_items else None
    return ok(
        request,
        JobProgress(
            job_id=job.id,
            status=job.status,
            total_items=job.total_items,
            processed_items=job.processed_items,
            succeeded_items=job.succeeded_items,
            failed_items=job.failed_items,
            percent_complete=percent,
            item_status_counts=counts,
            heartbeat_at=job.heartbeat_at,
            updated_at=job.updated_at,
        ),
    )


@router.post(
    "/{job_id}/retry", response_model=Envelope[JobOut], summary="Re-queue a failed, partial or cancelled job"
)
def retry_job(job_id: uuid.UUID, request: Request, session: SessionDep, user: ExecuteUser):
    job, previous = service.retry_job(session, job_id)
    audit.record(session, user.actor, "job.retry", "processing_job", job.id, "success",
                 {"job_type": job.job_type, "previous_status": previous})  # fmt: skip
    session.commit()
    return ok(request, to_out(job, user))


@router.post(
    "/{job_id}/cancel",
    response_model=Envelope[JobOut],
    summary="Cancel a queued job or request cancellation of a running one",
)
def cancel_job(job_id: uuid.UUID, request: Request, session: SessionDep, user: ExecuteUser):
    job, previous = service.cancel_job(session, job_id)
    audit.record(session, user.actor, "job.cancel", "processing_job", job.id, "success",
                 {"job_type": job.job_type, "previous_status": previous})  # fmt: skip
    session.commit()
    return ok(request, to_out(job, user))
