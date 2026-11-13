# ruff: noqa: E501  (operator tool: wide status/summary literals)
"""Operator commands for the fixed AI cohort run. No HTTP, no login: they act as the system actor and every
state change is written to the audit log. Nothing here calls a model provider; it only creates the
cohort manifest,
queues jobs (a worker does the paid work) and reports.

    uv run --directory backend python -m anticipa.evidence.ops cohort --seed 20261111 --size 10000 \\
        --dataset-version syn-20261108-s42
    uv run --directory backend python -m anticipa.evidence.ops submit-extraction --cohort-id <id>
    ANTICIPA_ENABLE_NEBIUS=1 uv run --directory backend python -m anticipa.evidence.ops submit-embedding \\
        --cohort-id <id> --approve-cost-usd 0.10
    uv run --directory backend python -m anticipa.evidence.ops status
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text

from anticipa.ai.config import load_config
from anticipa.ai.settings import get_ai_settings
from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.db import get_sessionmaker
from anticipa.evidence import service
from anticipa.evidence.cohort import (
    default_cohort_id,
    docgen_difficult_residents,
    load_manifest,
    save_manifest,
    select_cohort,
)
from anticipa.evidence.estimate import escalation_cap, estimate_extraction
from anticipa.jobs.models import ProcessingJob
from anticipa.jobs.service import submit_job

log = logging.getLogger("anticipa.evidence.ops")


def _dump(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))  # noqa: T201


def cmd_cohort(args: argparse.Namespace) -> int:
    store, sessions = service.get_store(), get_sessionmaker()
    cohort_id = args.cohort_id or default_cohort_id(args.seed, args.size, args.dataset_version)
    with sessions() as s:
        difficult = docgen_difficult_residents(args.dataset_version) if args.dataset_version else None
        manifest = select_cohort(
            s,
            seed=args.seed,
            size=args.size,
            dataset_version=args.dataset_version,
            difficult_resident_ids=difficult,
            difficult_share=args.difficult_share,
            cohort_id=cohort_id,
        )
        try:
            existing = load_manifest(store, cohort_id)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            same = existing.resident_ids_sha256 == manifest.resident_ids_sha256
            _dump({"status": "exists", "identical_to_fresh_selection": same, **existing.summary()})
            return 0 if same else 2  # a registered cohort is never silently replaced
        save_manifest(store, manifest)
        audit.record(
            s, Actor.system(), "evidence.cohort.create", "ai_cohort", cohort_id, "success",
            {"size": manifest.size, "seed": manifest.seed, "sha256": manifest.resident_ids_sha256,
             "dataset_version": manifest.dataset_version, "difficult_count": manifest.difficult_count},
        )  # fmt: skip
        s.commit()
        docs = service.count_documents(
            s, service.DocumentSelector(), pending_only=False, residents=manifest.resident_ids
        )
    _dump({"status": "created", "documents_in_cohort": docs, **manifest.summary()})
    return 0


def _extraction_plan(
    s: Any, cohort_id: str, prompt_ref: str = "evidence.extract@1", force: bool = False
) -> tuple[service.ExtractionScope, list, int, int, dict[str, Any]]:
    settings, cfg = get_ai_settings(), load_config()
    scope = service.ExtractionScope(cohort_id=cohort_id, prompt_ref=prompt_ref, force=force)
    residents = service.load_cohort_residents(scope, service.get_store())
    total = service.count_documents(s, scope, residents=residents, pending_only=not force)
    cohort_docs = service.count_documents(
        s, service.DocumentSelector(), pending_only=False, residents=residents
    )
    cap = escalation_cap(cohort_docs, 0, settings.anticipa_ai_escalation_share)
    est = estimate_extraction(cfg, total, cap, run_budget=settings.anticipa_ai_run_budget_usd)
    est["escalation_share"] = str(settings.anticipa_ai_escalation_share)
    est["daily_budget_usd"] = str(settings.anticipa_ai_daily_budget_usd)
    return scope, residents, total, cohort_docs, est


def cmd_submit_extraction(args: argparse.Namespace) -> int:
    with get_sessionmaker()() as s:
        scope, _, total, cohort_docs, est = _extraction_plan(s, args.cohort_id, args.prompt_ref, args.force)
        scope.concurrency = args.concurrency
        log.info("extraction estimate: %s", est)
        if not est["within_run_budget"]:
            _dump({"refused": "expected cost exceeds ANTICIPA_AI_RUN_BUDGET_USD", "estimate": est})
            return 2
        res = submit_job(
            s, "evidence.extract_batch", scope.model_dump(mode="json"),
            f"extract-{args.cohort_id}" + ("" if args.prompt_ref == "evidence.extract@1" else f"-{args.prompt_ref}"),
            config_version=args.prompt_ref,
        )  # fmt: skip
        if res.created:
            audit.record(
                s, Actor.system(), "evidence.extraction.submit", "processing_job", res.job.id, "success",
                {"documents": total, "cohort_id": args.cohort_id, "cohort_documents": cohort_docs,
                 "expected_usd": est["expected_total_usd"], "escalation_share": est["escalation_share"]},
            )  # fmt: skip
        s.commit()
        _dump({"job_id": str(res.job.id), "created": res.created, "status": res.job.status, "documents": total,
               "cohort_documents": cohort_docs, "estimate": est})  # fmt: skip
    return 0


def cmd_submit_embedding(args: argparse.Namespace) -> int:
    settings, cfg = get_ai_settings(), load_config()
    if "nebius" not in settings.enabled_providers:
        _dump({"refused": "Nebius is opt-in: set ANTICIPA_ENABLE_NEBIUS=1"})
        return 2
    scope = service.EmbeddingScope(
        cohort_id=args.cohort_id, approve_cost_usd=args.approve_cost_usd, concurrency=2
    )
    with get_sessionmaker()() as s:
        store = service.get_store()
        residents = service.load_cohort_residents(scope, store)
        est = service.estimate_embedding_scope(s, scope, residents, store, cfg)
        need = Decimal(est["estimated_cost_usd"])
        log.info("embedding estimate: %s", est)
        if args.approve_cost_usd is None or args.approve_cost_usd < need:
            _dump({"refused": f"approval required: estimate ${need}", "estimate": est})
            return 2
        res = submit_job(
            s, "evidence.embed_documents", scope.model_dump(mode="json"), f"embed-{args.cohort_id}"
        )
        if res.created:
            audit.record(
                s, Actor.system(), "evidence.embedding.submit", "processing_job", res.job.id, "success",
                {"cohort_id": args.cohort_id, "estimated_cost_usd": str(need), "approved_usd": str(args.approve_cost_usd)},
            )  # fmt: skip
        s.commit()
        _dump({"job_id": str(res.job.id), "created": res.created, "status": res.job.status, "estimate": est})
    return 0


def cmd_run_embedding(args: argparse.Namespace) -> int:
    """Queue the embedding job AND run it in this process (claimed before it is ever visible as
    `queued`), so a
    shared worker that has Nebius disabled can never claim and refuse it. Same approval and hard-stop
    rules."""
    from anticipa.evidence import handlers  # noqa: F401  (registers the evidence jobs in this process)
    from anticipa.jobs import service as jobs
    from anticipa.jobs.context import JobContext
    from anticipa.jobs.registry import get_handler
    from anticipa.jobs.worker import _Heartbeat, new_worker_id

    settings, cfg = get_ai_settings(), load_config()
    if "nebius" not in settings.enabled_providers:
        _dump({"refused": "Nebius is opt-in: set ANTICIPA_ENABLE_NEBIUS=1 for this process"})
        return 2
    scope = service.EmbeddingScope(
        cohort_id=args.cohort_id, approve_cost_usd=args.approve_cost_usd, concurrency=2
    )
    sessions, store, worker = get_sessionmaker(), service.get_store(), new_worker_id()
    with sessions() as s:
        residents = service.load_cohort_residents(scope, store)
        if args.no_precheck:  # the handler re-plans from the actual texts and enforces the approval itself
            est, need = (
                {"skipped": "precheck skipped; the job enforces approval >= its own estimate"},
                Decimal(0),
            )
        else:
            est = service.estimate_embedding_scope(s, scope, residents, store, cfg)
            need = Decimal(est["estimated_cost_usd"])
            log.info("embedding estimate: %s", est)
            if args.approve_cost_usd is None or args.approve_cost_usd < need:
                _dump({"refused": f"approval required: estimate ${need}", "estimate": est})
                return 2
        res = submit_job(
            s, "evidence.embed_documents", scope.model_dump(mode="json"), f"embed-{args.cohort_id}"
        )
        job = res.job
        if not res.created and job.status in ("completed", "running", "queued"):
            _dump({"job_id": str(job.id), "status": job.status, "note": "already submitted; nothing to do"})
            return 0
        audit.record(
            s, Actor.system(), "evidence.embedding.submit", "processing_job", job.id, "success",
            {"cohort_id": args.cohort_id, "estimated_cost_usd": str(need), "approved_usd": str(args.approve_cost_usd)},
        )  # fmt: skip
        job.status, job.locked_by = "running", worker  # claimed in the same transaction as the insert
        job.heartbeat_at = job.started_at = datetime.now(UTC)
        s.commit()
        s.refresh(job)
        s.expunge(job)
    beat = _Heartbeat(sessions, job.id, worker, 10.0)
    beat.start()
    try:
        get_handler("evidence.embed_documents")(JobContext(sessions, job, worker))
    except Exception as exc:  # noqa: BLE001 - recorded on the job, then reported
        log.exception("embedding run failed")
        with sessions() as s:
            jobs.finish_failure(s, job.id, worker, exc, permanent=True)
        return 1
    finally:
        beat.stop()
    with sessions() as s:
        status = jobs.finish_success(s, job.id, worker)
    _dump({"job_id": str(job.id), "status": status, "estimate": est})
    return 0


def cmd_normalise(args: argparse.Namespace) -> int:
    """Re-apply the deterministic normaliser (no model call) to the cohort's existing machine facts, document by
    document (the section-heading rule needs the text). Originals stay in value["normaliser"]; re-running (or a new
    version) derives from those originals. Human-reviewed facts are never touched."""
    from collections import Counter

    from sqlalchemy import update

    from anticipa.datafabric.models import Document
    from anticipa.evidence.models import EvidenceFact, EvidenceSource
    from anticipa.evidence.normalise import (
        NORMALISER_VERSION,
        apply_to_value,
        normalise,
        originals,
        section_heading,
    )

    scope = service.DocumentSelector(cohort_id=args.cohort_id)
    residents = service.load_cohort_residents(scope, service.get_store())
    store = service.get_store()
    seen = changed = unreadable = 0
    rules: Counter = Counter()
    moves: Counter = Counter()
    last = None
    with get_sessionmaker()() as s:
        while True:
            stmt = select(Document.id, Document.storage_key, Document.created_on).where(
                Document.resident_id.in_(residents)
            )
            stmt = stmt.order_by(Document.id).limit(200)
            if last is not None:
                stmt = stmt.where(Document.id > last)
            docs = s.execute(stmt).all()
            if not docs:
                break
            last = docs[-1].id
            by_id = {d.id: d for d in docs}
            facts = s.execute(
                select(
                    EvidenceFact.id, EvidenceFact.category, EvidenceFact.temporal_status, EvidenceFact.assertion_status,
                    EvidenceFact.fact_type, EvidenceFact.subject_type, EvidenceFact.value,
                    EvidenceSource.document_id, EvidenceSource.source_start_offset,
                )
                .join(EvidenceSource, EvidenceSource.evidence_fact_id == EvidenceFact.id)
                .where(EvidenceSource.document_id.in_(list(by_id)), EvidenceFact.review_status.in_(service.MACHINE_REVIEW_STATES))
            ).all()  # fmt: skip
            texts: dict[Any, str | None] = {}
            batch: list[dict[str, Any]] = []
            for r in facts:
                seen += 1
                d = by_id[r.document_id]
                cat0, temp0 = originals(r.category, r.temporal_status, r.value or {})
                base = {k: v for k, v in (r.value or {}).items() if k != "normaliser"}
                heading = None
                if (
                    r.source_start_offset is not None and cat0 == "condition"
                ):  # the only category T2 can change
                    if d.id not in texts:
                        try:
                            texts[d.id] = store.read_text(d.storage_key)
                        except Exception:  # noqa: BLE001
                            texts[d.id] = None
                            unreadable += 1
                    if texts[d.id] is not None:
                        heading = section_heading(texts[d.id], r.source_start_offset)
                norm = normalise(cat0, temp0, r.assertion_status, r.fact_type, base, d.created_on.date() if d.created_on else None,
                                 subject_type=r.subject_type, heading=heading)  # fmt: skip
                new_value = apply_to_value(base, norm, cat0, temp0)
                if (norm.category, norm.temporal_status, new_value) == (
                    r.category,
                    r.temporal_status,
                    r.value,
                ):
                    continue
                changed += 1
                rules.update(norm.rules)
                moves[
                    f"{cat0}->{norm.category}"
                    if norm.category != cat0
                    else f"temporal {temp0}->{norm.temporal_status}"
                ] += 1
                batch.append(
                    {
                        "id": r.id,
                        "category": norm.category,
                        "temporal_status": norm.temporal_status,
                        "value": new_value,
                    }
                )
            if batch and not args.dry_run:
                s.execute(update(EvidenceFact), batch)
                s.commit()
        if not args.dry_run and changed:
            audit.record(s, Actor.system(), "evidence.normalise", "ai_cohort", args.cohort_id, "success",
                         {"version": NORMALISER_VERSION, "facts_seen": seen, "facts_changed": changed, "rules": dict(rules)})  # fmt: skip
            s.commit()
    _dump({"version": NORMALISER_VERSION, "dry_run": args.dry_run, "facts_seen": seen, "facts_changed": changed,
           "unreadable_documents": unreadable, "rules": dict(rules), "moves": dict(moves.most_common())})  # fmt: skip
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = get_ai_settings()
    with get_sessionmaker()() as s:
        stmt = (
            select(ProcessingJob)
            .where(ProcessingJob.job_type.like("evidence.%"))
            .order_by(ProcessingJob.created_at.desc())
        )
        if args.job_id:
            stmt = select(ProcessingJob).where(ProcessingJob.id == args.job_id)
        jobs = s.execute(stmt.limit(args.limit)).scalars().all()
        now = datetime.now(UTC)
        out: list[dict[str, Any]] = []
        for j in jobs:
            cp = j.checkpoint or {}
            started = j.started_at
            elapsed = (now - started).total_seconds() if started else None
            done = j.processed_items
            rate = round(done / elapsed * 60, 1) if elapsed and done else None
            eta_min = (
                round((j.total_items - done) / (done / elapsed) / 60, 1)
                if elapsed and done and j.total_items
                else None
            )
            out.append({
                "job_id": str(j.id), "type": j.job_type, "status": j.status, "total": j.total_items, "processed": done,
                "succeeded": j.succeeded_items, "failed": j.failed_items, "docs_per_min": rate, "eta_minutes": eta_min,
                "cost_usd": cp.get("cost_usd"), "tokens": cp.get("tokens"), "by_route": cp.get("by_route"),
                "escalation": cp.get("escalation"), "escalation_cap": cp.get("escalation_cap"),
                "run_budget_remaining_usd": cp.get("run_budget_remaining_usd"), "stopped_reason": cp.get("stopped_reason"),
                "stopped_detail": cp.get("stopped_detail"), "refused": cp.get("refused"),
                "retry_count": j.retry_count, "error_summary": j.error_summary, "heartbeat_at": j.heartbeat_at,
            })  # fmt: skip
        spend = {
            r.provider: {"cost_usd": str(r.cost), "calls": r.calls, "input_tokens": r.i, "output_tokens": r.o}
            for r in s.execute(
                text(
                    "SELECT provider, coalesce(sum(cost_usd),0) AS cost, count(*) AS calls, "
                    "coalesce(sum(input_tokens),0)+coalesce(sum(cache_read_tokens),0)+coalesce(sum(cache_write_tokens),0) AS i, "
                    "coalesce(sum(output_tokens),0) AS o FROM model_invocations GROUP BY provider"
                )
            )
        }
        today = s.execute(
            text(
                "SELECT coalesce(sum(cost_usd),0) FROM model_invocations WHERE created_at >= date_trunc('day', now() at time zone 'utc')"
            )
        ).scalar_one()
        facts = dict(
            s.execute(text("SELECT validation_status, count(*) FROM evidence_facts GROUP BY 1")).all()
        )
        cache_hits = s.execute(text("SELECT count(*) FROM model_invocations WHERE cache_hit")).scalar_one()
    _dump({
        "jobs": out, "spend_by_provider": spend, "spend_today_usd": str(today),
        "nebius_credit_usd": str(settings.anticipa_nebius_credit_usd),
        "daily_budget_usd": str(settings.anticipa_ai_daily_budget_usd),
        "evidence_facts_by_validation_status": facts, "cache_hit_invocations": cache_hits,
    })  # fmt: skip
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cohort", help="create (or re-read) the seeded, stratified AI cohort manifest")
    c.add_argument("--seed", type=int, required=True)
    c.add_argument("--size", type=int, default=10_000)
    c.add_argument("--dataset-version", default=None)
    c.add_argument("--difficult-share", type=float, default=0.20)
    c.add_argument("--cohort-id", default=None)
    c.set_defaults(func=cmd_cohort)
    e = sub.add_parser(
        "submit-extraction", help="queue evidence.extract_batch for a cohort (a worker runs it)"
    )
    e.add_argument("--cohort-id", required=True)
    e.add_argument("--concurrency", type=int, default=8)
    e.add_argument(
        "--prompt-ref",
        default="evidence.extract@1",
        help="prompt file to extract with, e.g. evidence.extract@4",
    )
    e.add_argument(
        "--force", action="store_true", help="re-extract documents already extracted (new prompt run)"
    )
    e.set_defaults(func=cmd_submit_extraction)
    m = sub.add_parser(
        "submit-embedding", help="queue evidence.embed_documents (Nebius, opt-in, needs approval)"
    )
    m.add_argument("--cohort-id", required=True)
    m.add_argument("--approve-cost-usd", type=Decimal, default=None)
    m.set_defaults(func=cmd_submit_embedding)
    r = sub.add_parser(
        "run-embedding", help="queue AND run evidence.embed_documents here (Nebius opt-in, approved cost)"
    )
    r.add_argument("--cohort-id", required=True)
    r.add_argument("--approve-cost-usd", type=Decimal, default=None)
    r.add_argument(
        "--no-precheck",
        action="store_true",
        help="skip the slow read-only estimate pass (the job still enforces it)",
    )
    r.set_defaults(func=cmd_run_embedding)
    nm = sub.add_parser(
        "normalise", help="re-apply the deterministic normaliser to the cohort's existing facts"
    )
    nm.add_argument("--cohort-id", required=True)
    nm.add_argument("--dry-run", action="store_true")
    nm.set_defaults(func=cmd_normalise)
    st = sub.add_parser("status", help="job progress, throughput, ETA, spend, review-queue counts")
    st.add_argument("--job-id", default=None)
    st.add_argument("--limit", type=int, default=5)
    st.set_defaults(func=cmd_status)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
