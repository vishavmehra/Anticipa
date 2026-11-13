"""Worker loop: claim (SKIP LOCKED) -> run handler with heartbeat -> finish; recovers stale locks; bounded
retries."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import func, update
from sqlalchemy.orm import Session, sessionmaker

from anticipa.config import Settings
from anticipa.core.modules import load_module_files
from anticipa.jobs import builtin as _builtin  # noqa: F401  (registers system.noop)
from anticipa.jobs import service
from anticipa.jobs.context import JobCancelled, JobContext, JobLockLost
from anticipa.jobs.models import ProcessingJob
from anticipa.jobs.registry import get_handler, registered_job_types

log = logging.getLogger("anticipa.worker")


@dataclass(frozen=True)
class WorkerConfig:
    poll_interval: float = 1.0
    heartbeat_interval: float = 10.0
    stale_after: float = 90.0
    backoff_base: float = 5.0
    backoff_cap: float = 300.0

    @classmethod
    def from_settings(cls, s: Settings) -> WorkerConfig:
        return cls(
            s.job_poll_interval_seconds,
            s.job_heartbeat_seconds,
            s.job_stale_after_seconds,
            s.job_backoff_base_seconds,
            s.job_backoff_cap_seconds,
        )


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class _Heartbeat(threading.Thread):
    def __init__(
        self, sessions: sessionmaker[Session], job_id: uuid.UUID, worker_id: str, interval: float
    ) -> None:
        super().__init__(name=f"heartbeat-{job_id}", daemon=True)
        self._args = (sessions, job_id, worker_id)
        self._interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        sessions, job_id, worker_id = self._args
        while not self._stop_event.wait(self._interval):
            try:
                with sessions() as s:
                    s.execute(
                        update(ProcessingJob)
                        .where(ProcessingJob.id == job_id, ProcessingJob.locked_by == worker_id)
                        .values(heartbeat_at=func.now())
                    )
                    s.commit()
            except Exception:
                log.exception("heartbeat failed", extra={"job_id": str(job_id)})

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)


def _refresh_analytics(sessions: sessionmaker[Session], job_type: str) -> None:
    """Refresh the precomputed analytics after a job of an analytics relevant module; never fails the job."""
    try:
        from anticipa.analytics.snapshot import after_job

        after_job(sessions, job_type)
    except Exception:
        log.exception("analytics refresh hook failed", extra={"job_type": job_type})


def run_one(sessions: sessionmaker[Session], worker_id: str, cfg: WorkerConfig) -> bool:
    """Claim and run at most one job. Returns False when nothing was runnable."""
    with sessions() as s:
        job = service.claim_next(s, worker_id)
    if job is None:
        return False
    extra = {"job_id": str(job.id), "job_type": job.job_type}
    log.info("job started", extra=extra)

    def finish(fn, *args, **kwargs) -> None:  # each finish call uses a fresh session
        with sessions() as s:
            if fn(s, job.id, worker_id, *args, **kwargs) is None:
                log.warning("job no longer owned by this worker; result discarded", extra=extra)

    handler = get_handler(job.job_type)
    if handler is None:
        finish(
            service.finish_failure, f"No handler registered for job type {job.job_type!r}.", permanent=True
        )
        log.error("no handler registered", extra=extra)
        return True

    heartbeat = _Heartbeat(sessions, job.id, worker_id, cfg.heartbeat_interval)
    heartbeat.start()
    try:
        handler(JobContext(sessions, job, worker_id))
    except JobLockLost:
        log.warning("job lock lost; stopping", extra=extra)
    except JobCancelled:
        finish(service.finish_cancelled)
        log.info("job cancelled", extra=extra)
    except Exception as exc:
        log.exception("job handler failed", extra=extra)  # stack goes to the log only
        finish(service.finish_failure, exc, backoff_base=cfg.backoff_base, backoff_cap=cfg.backoff_cap)
    else:
        finish(service.finish_success)
        log.info("job finished", extra=extra)
        _refresh_analytics(sessions, job.job_type)
    finally:
        heartbeat.stop()
    return True


def run_worker(
    sessions: sessionmaker[Session],
    cfg: WorkerConfig,
    stop: threading.Event,
    *,
    worker_id: str | None = None,
    exit_when_idle: bool = False,
) -> int:
    """Run until `stop` is set (or idle, if `exit_when_idle`). Returns the number of jobs processed."""
    worker_id = worker_id or new_worker_id()
    load_module_files("handlers")
    log.info("worker started", extra={"worker_id": worker_id, "job_types": registered_job_types()})
    done = 0
    last_sweep = 0.0
    while not stop.is_set():
        if time.monotonic() - last_sweep >= max(cfg.stale_after / 3, 1.0):
            last_sweep = time.monotonic()
            with sessions() as s:
                recovered = service.requeue_stale(
                    s, cfg.stale_after, backoff_base=cfg.backoff_base, backoff_cap=cfg.backoff_cap
                )
            if recovered:
                log.warning("recovered stale jobs", extra={"job_ids": [str(j) for j in recovered]})
        try:
            ran = run_one(sessions, worker_id, cfg)
        except Exception:  # DB outage etc.: back off, keep the loop alive
            log.exception("worker iteration failed")
            ran = False
        if ran:
            done += 1
            continue
        if exit_when_idle:
            break
        stop.wait(cfg.poll_interval)
    log.info("worker stopped", extra={"worker_id": worker_id, "jobs_processed": done})
    return done


class EmbeddedWorker:
    """`run_worker` on a background thread inside the API process (ANTICIPA_EMBEDDED_WORKER=1; single
    concurrency).

    Same claim loop and heartbeats as `anticipa worker`. `stop()` asks the loop to exit between jobs and waits
    up to
    `stop_timeout`; a handler that outlives that is abandoned (daemon thread) and its job is requeued by the
    stale-heartbeat sweep (so a worker that restarts after the host sleeps recovers in-flight work).
    """

    def __init__(
        self, sessions: sessionmaker[Session], cfg: WorkerConfig, *, stop_timeout: float = 20.0
    ) -> None:
        self._stop_timeout = stop_timeout
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(sessions, cfg), name="embedded-worker", daemon=True
        )

    def _run(self, sessions: sessionmaker[Session], cfg: WorkerConfig) -> None:
        try:
            run_worker(sessions, cfg, self._stop_event)
        except Exception:
            log.exception("embedded worker crashed")

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=self._stop_timeout)
        if self._thread.is_alive():
            log.warning("embedded worker still running a job at shutdown; it will be requeued as stale")
