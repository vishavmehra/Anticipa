# ruff: noqa: E501
"""Job handler `docgen.generate_batch` (contract 17 §4). Loaded by the worker once "datafabric.docgen" is in MODULES.

input_scope: dataset_version (required), seed (required), residents (int | "all", default "all"), source ("db"),
chunk_size (500), enrich (true: plant seeded screening completions/exclusions, enrich.py),
family (linked|all|none; default linked) and only_linked (skip residents outside family-linked blocks). Re-submitting is safe: residents already in the ground-truth ledger are skipped and documents are
registered with ON CONFLICT DO NOTHING, so a retry after a crash never duplicates anything.
"""

from __future__ import annotations

from sqlalchemy import func, select

from anticipa.datafabric.docgen.generate import RunReport, generate
from anticipa.datafabric.models import Dataset, Resident
from anticipa.jobs.context import JobContext
from anticipa.jobs.registry import register_job


@register_job("docgen.generate_batch")
def generate_batch(ctx: JobContext) -> None:
    scope = ctx.job.input_scope
    version, seed = scope["dataset_version"], int(scope["seed"])
    wanted = scope.get("residents", "all")
    limit = None if wanted in (None, "all") else int(wanted)
    source = scope.get("source", "db")
    with ctx.session() as s:
        total = s.execute(
            select(func.count())
            .select_from(Resident)
            .join(Dataset, Dataset.id == Resident.dataset_id)
            .where(Dataset.dataset_version == version)
        ).scalar_one()
    ctx.progress(total=min(total, limit) if limit is not None else total)
    prev = {"seen": 0}

    def on_chunk(report: RunReport, last_id) -> None:
        delta = report.residents_seen - prev["seen"]
        prev["seen"] = report.residents_seen
        ctx.progress(processed=delta, succeeded=delta)
        ctx.checkpoint(
            {
                "last_resident_id": str(last_id),
                "residents_generated": report.residents_generated,
                "documents": report.documents,
                "documents_registered": report.documents_registered,
            }
        )

    generate(
        version,
        seed=seed,
        residents=limit,
        source=source,
        chunk_size=int(scope.get("chunk_size", 500)),
        session_factory=ctx.session,
        enrich=bool(scope.get("enrich", True)),
        family=scope.get("family"),
        only_linked=bool(scope.get("only_linked", False)),
        refresh_linked=bool(scope.get("refresh_linked", False)),
        on_chunk=on_chunk,
        cancelled=ctx.cancel_requested,
    )
    ctx.raise_if_cancelled()
