"""JobContext: what a job handler may do (contract 17 §4). Every write is its own short transaction and is
fenced on `locked_by`, so a worker that lost its lock (stale requeue) cannot clobber the job."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from anticipa.jobs.models import ProcessingItem, ProcessingJob

MAX_ERROR_CHARS = 500


class JobCancelled(Exception):
    """Raise (or let `ctx.raise_if_cancelled()` raise) to stop a job cooperatively; the job ends
    `cancelled`."""


class JobLockLost(Exception):
    """This worker no longer owns the job (requeued as stale, or cancelled underneath it). Stop
    immediately."""


def short_error(exc_or_text: BaseException | str) -> str:
    text = (
        f"{type(exc_or_text).__name__}: {exc_or_text}"
        if isinstance(exc_or_text, BaseException)
        else str(exc_or_text)
    )
    return text[:MAX_ERROR_CHARS]


@dataclass
class JobItem:
    """Idempotent per-item status. Call `succeed`/`fail`/`skip` once the item has been handled."""

    key: str
    status: str
    attempts: int
    _ctx: JobContext

    @property
    def done(self) -> bool:
        return self.status in ("succeeded", "skipped")

    def succeed(self) -> None:
        self._set("succeeded", None)

    def skip(self) -> None:
        self._set("skipped", None)

    def fail(self, error: BaseException | str) -> None:
        self._set("failed", short_error(error))

    def _set(self, status: str, error: str | None) -> None:
        with self._ctx.session() as s:
            self._ctx._assert_owned(s)
            s.execute(
                update(ProcessingItem)
                .where(ProcessingItem.job_id == self._ctx.job_id, ProcessingItem.item_key == self.key)
                .values(
                    status=status, error=error, attempts=ProcessingItem.attempts + 1, updated_at=func.now()
                )
            )
            s.commit()
        self.status = status
        self.attempts += 1


class JobContext:
    def __init__(self, sessions: sessionmaker[Session], job: ProcessingJob, worker_id: str) -> None:
        self._sessions = sessions
        self.job = (
            job  # detached snapshot taken when the job was claimed (input_scope, checkpoint, retry_count...)
        )
        self.job_id: uuid.UUID = job.id
        self.worker_id = worker_id

    def session(self) -> Session:
        """A new SQLAlchemy session: `with ctx.session() as s: ...` (caller commits)."""
        return self._sessions()

    def _assert_owned(self, s: Session) -> None:
        owner = s.execute(
            select(ProcessingJob.locked_by).where(ProcessingJob.id == self.job_id).with_for_update()
        ).scalar_one_or_none()
        if owner != self.worker_id:
            raise JobLockLost(str(self.job_id))

    def checkpoint(self, data: dict[str, Any]) -> None:
        """Replace the job checkpoint (persist + commit) and refresh the heartbeat."""
        with self.session() as s:
            self._assert_owned(s)
            s.execute(
                update(ProcessingJob)
                .where(ProcessingJob.id == self.job_id)
                .values(checkpoint=data, heartbeat_at=func.now(), updated_at=func.now())
            )
            s.commit()

    def progress(
        self, *, processed: int = 0, succeeded: int = 0, failed: int = 0, total: int | None = None
    ) -> None:
        """Add deltas to the counters (negative allowed, e.g. a failed item later succeeds); `total` sets
        total_items."""
        values: dict[str, Any] = {
            "processed_items": ProcessingJob.processed_items + processed,
            "succeeded_items": ProcessingJob.succeeded_items + succeeded,
            "failed_items": ProcessingJob.failed_items + failed,
            "heartbeat_at": func.now(),
            "updated_at": func.now(),
        }
        if total is not None:
            values["total_items"] = total
        with self.session() as s:
            self._assert_owned(s)
            s.execute(update(ProcessingJob).where(ProcessingJob.id == self.job_id).values(**values))
            s.commit()

    def item(self, item_key: str) -> JobItem:
        """Get (creating as `pending` if new) the status row for `item_key`; safe to call on every retry."""
        with self.session() as s:
            self._assert_owned(s)
            s.execute(
                pg_insert(ProcessingItem)
                .values(job_id=self.job_id, item_key=item_key)
                .on_conflict_do_nothing(index_elements=["job_id", "item_key"])
            )
            row = s.execute(
                select(ProcessingItem).where(
                    ProcessingItem.job_id == self.job_id, ProcessingItem.item_key == item_key
                )
            ).scalar_one()
            s.commit()
            return JobItem(key=item_key, status=row.status, attempts=row.attempts, _ctx=self)

    def cancel_requested(self) -> bool:
        with self.session() as s:
            return bool(
                s.execute(
                    select(ProcessingJob.cancel_requested).where(ProcessingJob.id == self.job_id)
                ).scalar()
            )

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested():
            raise JobCancelled(str(self.job_id))
