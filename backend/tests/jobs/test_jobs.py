"""Job state machine and worker loop (contract 17 section 4) on the test database. Everything runs in the
test's
rolled-back transaction; created_at is pinned to the epoch so these jobs are always claimed first."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import sessionmaker

from anticipa.core.errors import Conflict, InvalidTransition, NotFound, ValidationFailed
from anticipa.jobs import service
from anticipa.jobs.models import ProcessingItem, ProcessingJob
from anticipa.jobs.registry import JOB_TYPE_RE, register_job
from anticipa.jobs.worker import WorkerConfig, run_one, run_worker

CFG = WorkerConfig(
    poll_interval=0.0, heartbeat_interval=3600.0, stale_after=60.0, backoff_base=0.0, backoff_cap=0.0
)


@pytest.fixture
def sessions(db_session) -> sessionmaker:
    return sessionmaker(
        bind=db_session.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def submit(db_session, job_type="system.noop", **kw):
    result = service.submit_job(db_session, job_type, **kw)
    db_session.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == result.job.id)
        .values(created_at=text("'1970-01-01 00:00:00+00'"))
    )
    db_session.commit()
    return result


def reload(db_session, job):
    db_session.expire_all()
    return db_session.get(ProcessingJob, job.id, populate_existing=True)


# ------------------------------------------------------------------------------------------------ submit


def test_submit_queues_a_job_and_idempotency_key_returns_the_existing_one(db_session):
    first = service.submit_job(db_session, "system.noop", {"items": 1}, idempotency_key="k-1")
    again = service.submit_job(db_session, "system.noop", {"items": 99}, idempotency_key="k-1")
    assert first.created and first.job.status == "queued"
    assert not again.created and again.job.id == first.job.id and again.job.input_scope == {"items": 1}
    with pytest.raises(Conflict):
        service.submit_job(db_session, "system.other", idempotency_key="k-1")


@pytest.mark.parametrize("bad", ["noop", "System.noop", "system.", "a b.c", "../x.y", ""])
def test_job_types_must_be_namespaced(db_session, bad):
    with pytest.raises(ValidationFailed):
        service.submit_job(db_session, bad)
    assert not JOB_TYPE_RE.match(bad)
    with pytest.raises(ValueError, match="namespaced"):
        register_job(bad)


def test_registering_the_same_job_type_twice_from_different_functions_fails():
    with pytest.raises(ValueError, match="already registered"):
        register_job("system.noop")(lambda ctx: None)


def test_get_job_unknown_is_not_found(db_session):
    with pytest.raises(NotFound):
        service.get_job(db_session, uuid.uuid4())


# ------------------------------------------------------------------------------------------------ run


def test_noop_job_runs_to_completion_with_item_rows(db_session, sessions):
    job = submit(db_session, input_scope={"items": 3}).job
    assert run_one(sessions, "w1", CFG) is True
    job = reload(db_session, job)
    assert (job.status, job.total_items, job.processed_items, job.succeeded_items, job.failed_items) == (
        "completed",
        3,
        3,
        3,
        0,
    )
    assert job.locked_by is None and job.completed_at is not None and job.checkpoint == {"last_index": 2}
    items = (
        db_session.execute(select(ProcessingItem.status).where(ProcessingItem.job_id == job.id))
        .scalars()
        .all()
    )
    assert items == ["succeeded"] * 3


def test_failed_items_mean_partially_completed_never_completed(db_session, sessions):
    job = submit(db_session, input_scope={"items": 3, "fail_items": [1]}).job
    run_one(sessions, "w1", CFG)
    job = reload(db_session, job)
    assert (job.status, job.succeeded_items, job.failed_items) == ("partially_completed", 2, 1)


def test_retry_after_partial_resumes_without_redoing_succeeded_items(db_session, sessions):
    job = submit(db_session, input_scope={"items": 3, "fail_items": [1]}).job
    run_one(sessions, "w1", CFG)
    db_session.execute(
        update(ProcessingJob).where(ProcessingJob.id == job.id).values(input_scope={"items": 3})
    )
    db_session.commit()
    retried, previous = service.retry_job(db_session, job.id)
    db_session.commit()
    assert previous == "partially_completed" and retried.status == "queued" and retried.retry_count == 0
    run_one(sessions, "w1", CFG)
    job = reload(db_session, job)
    assert (job.status, job.processed_items, job.succeeded_items, job.failed_items) == ("completed", 3, 3, 0)


def test_handler_exception_retries_with_backoff_then_fails_permanently(db_session, sessions):
    job = submit(db_session, input_scope={"always_raise": True}, max_retries=1).job
    run_one(sessions, "w1", CFG)
    job = reload(db_session, job)
    assert job.status == "queued" and job.retry_count == 1 and "always_raise" in job.error_summary
    run_one(sessions, "w1", CFG)
    job = reload(db_session, job)
    assert job.status == "failed" and job.retry_count == 2 and job.completed_at is not None


def test_transient_failure_recovers_on_automatic_retry(db_session, sessions):
    job = submit(db_session, input_scope={"items": 2, "raise_until_retry": 1}).job
    run_one(sessions, "w1", CFG)
    assert reload(db_session, job).status == "queued"
    run_one(sessions, "w1", CFG)
    assert reload(db_session, job).status == "completed"


def test_backoff_gates_the_next_claim(db_session, sessions):
    job = submit(db_session, input_scope={"always_raise": True}).job
    run_one(sessions, "w1", WorkerConfig(0.0, 3600.0, 60.0, backoff_base=3600.0, backoff_cap=3600.0))
    job = reload(db_session, job)
    assert job.status == "queued" and job.next_run_at is not None
    claimed = service.claim_next(db_session, "w2")  # not claimable before next_run_at
    assert claimed is None or claimed.id != job.id
    assert reload(db_session, job).status == "queued"


def test_unknown_job_type_fails_permanently(db_session, sessions):
    job = submit(db_session, "system.nobody_handles_this").job
    run_one(sessions, "w1", CFG)
    job = reload(db_session, job)
    assert job.status == "failed" and "No handler registered" in job.error_summary


def test_claim_is_exclusive_and_ordered(db_session):
    a = submit(db_session).job
    b = submit(db_session).job
    first = service.claim_next(db_session, "w1")
    second = service.claim_next(db_session, "w2")
    assert {first.id, second.id} == {a.id, b.id} and first.status == "running" and first.locked_by == "w1"


def test_run_worker_drains_the_queue_and_exits_when_idle(db_session, sessions, monkeypatch):
    import threading

    monkeypatch.setattr(
        "anticipa.jobs.worker.load_module_files", lambda _name: []
    )  # only system.noop is needed

    submit(db_session, input_scope={"items": 1})
    submit(db_session, input_scope={"items": 1})
    assert run_worker(sessions, CFG, threading.Event(), worker_id="w1", exit_when_idle=True) >= 2


# ------------------------------------------------------------------------------------------------ cancel /
# stale


def test_queued_job_cancels_immediately_and_terminal_jobs_cannot(db_session):
    job = submit(db_session).job
    cancelled, previous = service.cancel_job(db_session, job.id)
    assert previous == "queued" and cancelled.status == "cancelled" and cancelled.completed_at is not None
    with pytest.raises(InvalidTransition):
        service.cancel_job(db_session, job.id)
    queued = service.submit_job(db_session, "system.noop").job
    with pytest.raises(
        InvalidTransition
    ):  # only failed / partially_completed / cancelled jobs can be retried
        service.retry_job(db_session, queued.id)


def test_running_job_stops_cooperatively_when_cancel_is_requested(db_session, sessions):
    job = submit(db_session, input_scope={"items": 5, "sleep_per_item": 0.0}).job
    claimed = service.claim_next(db_session, "w1")
    assert claimed.id == job.id
    service.cancel_job(db_session, job.id)  # running -> cancel_requested
    assert reload(db_session, job).status == "running" and reload(db_session, job).cancel_requested
    db_session.execute(
        update(ProcessingJob).where(ProcessingJob.id == job.id).values(status="queued", locked_by=None)
    )
    db_session.commit()  # simulate the worker picking it up after the request
    run_one(sessions, "w1", CFG)
    assert reload(db_session, job).status == "cancelled"


def test_stale_running_jobs_are_requeued_as_a_failed_attempt(db_session):
    job = submit(db_session).job
    service.claim_next(db_session, "dead-worker")
    db_session.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == job.id)
        .values(heartbeat_at=func.now() - timedelta(hours=1))
    )
    db_session.commit()
    assert job.id in service.requeue_stale(db_session, 60.0, backoff_base=0.0, backoff_cap=0.0)
    job = reload(db_session, job)
    assert (
        job.status == "queued"
        and job.retry_count == 1
        and "heartbeat" in job.error_summary
        and job.locked_by is None
    )


def test_a_worker_that_lost_the_lock_cannot_finish_the_job(db_session):
    job = submit(db_session).job
    service.claim_next(db_session, "w1")
    assert service.finish_success(db_session, job.id, "someone-else") is None
    assert reload(db_session, job).status == "running"
    assert service.finish_success(db_session, job.id, "w1") == "completed"
