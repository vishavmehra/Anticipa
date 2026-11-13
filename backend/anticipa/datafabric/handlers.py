"""Job handler `datafabric.ingest_batch` (contract 17 §4): load generated Synthea batches into the database.

input_scope:
  dataset_version (str, required)   e.g. "syn-20261108-s42" (must already be generated under data/synthea/)
  batches (list[int], optional)     batch indexes; default every completed batch of the dataset
  force (bool, optional)            re-run batches already recorded as ingested (inserts stay idempotent)
Each batch is one job item (`batch-<i>`), so a retried job skips batches that already succeeded.
"""

from __future__ import annotations

import re

from anticipa.core.errors import ValidationFailed
from anticipa.datafabric.loader import ingest_batch
from anticipa.datafabric.synthea import SYNTHEA_DIR, load_manifest, sorted_batches
from anticipa.jobs.context import JobContext, short_error
from anticipa.jobs.registry import register_job

_VERSION_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
)  # no path separators: the version names a data directory


@register_job("datafabric.ingest_batch")
def ingest_batches(ctx: JobContext) -> None:
    scope = ctx.job.input_scope
    version = scope.get("dataset_version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise ValidationFailed("input_scope.dataset_version is required (letters, digits, '.', '_', '-')")
    manifest = load_manifest(SYNTHEA_DIR / version)
    if manifest is None:
        raise ValidationFailed(f"dataset {version!r} has not been generated (no manifest.json)")
    completed = [i for i, b in sorted_batches(manifest) if b["status"] == "completed"]
    requested = scope.get("batches")
    if requested is not None:
        if not isinstance(requested, list) or not all(
            isinstance(i, int) and i in completed for i in requested
        ):
            raise ValidationFailed("input_scope.batches must list completed batch indexes")
        completed = requested
    force = bool(scope.get("force", False))

    ctx.progress(total=len(completed))
    for i in completed:
        ctx.raise_if_cancelled()
        item = ctx.item(f"batch-{i}")
        if item.done:
            continue
        previously_failed = item.status == "failed"
        try:
            with ctx.session() as session:
                ingest_batch(
                    session,
                    dataset_version=version,
                    batch_index=i,
                    manifest=manifest,
                    data_dir=SYNTHEA_DIR,
                    force=force,
                )
        except Exception as exc:  # noqa: BLE001 - recorded on the item; the job ends partially_completed
            item.fail(short_error(exc))
            if not previously_failed:
                ctx.progress(processed=1, failed=1)
        else:
            item.succeed()
            ctx.progress(
                processed=0 if previously_failed else 1, succeeded=1, failed=-1 if previously_failed else 0
            )
        ctx.checkpoint({"last_batch": i})
