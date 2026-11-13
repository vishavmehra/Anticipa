"""Background jobs of the evidence module (contract 17 §4). Handlers are sync (the worker is threaded); each
runs one asyncio loop for the Model Router and a bounded worker pool.

  evidence.extract_batch      cohort documents -> Haiku (evidence.extract) -> quote verification -> acceptance
                              policy; deterministic triggers may re-run <= 10% of the cohort's documents
                              on Sonnet
                              (evidence.extract.complex) -> evidence_facts + evidence_sources
  evidence.embed_documents    OPT-IN (Nebius): cohort documents -> chunks -> unique texts missing an
  embedding ->
                              estimate + approval -> router.embed (1024-d) -> document_chunks

Both are idempotent per item (document id + content hash + prompt/model), resumable (`ctx.item`), cancellable,
bound to the registered AI cohort, and stop cleanly on `budget_exceeded` / `auth` instead of burning retries.
Document text is read from the document store, never logged, and only the Model Router talks to a provider.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from decimal import Decimal
from functools import partial
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from anticipa.ai import EmbedRequest, RouterError
from anticipa.ai.config import load_config
from anticipa.ai.settings import get_ai_settings
from anticipa.ai.sinks import DbSink
from anticipa.consent.service import ai_processing_denied
from anticipa.datafabric.models import DocumentChunk
from anticipa.evidence import service
from anticipa.evidence.chunking import Chunk, chunk_text
from anticipa.evidence.escalation import (
    CONFLICTING_FAMILY_HISTORY,
    EscalationBudget,
    escalation_triggers,
    flag_conflicts_for_review,
)
from anticipa.evidence.estimate import estimate_embedding, estimate_extraction
from anticipa.evidence.extract import DocumentMeta, ExtractionOutcome, extract_document
from anticipa.jobs.context import JobCancelled, JobContext, JobLockLost
from anticipa.jobs.models import ProcessingItem
from anticipa.jobs.registry import register_job

log = logging.getLogger("anticipa.evidence.jobs")
_END = object()
STANDARD, COMPLEX = "evidence.extract", "evidence.extract.complex"
# Retrying the next document would fail the same way. `auth` only counts after AUTH_STRIKES consecutive
# documents: a missing key for one provider is not an outage.
FATAL_CATEGORIES = frozenset({"budget_exceeded", "auth"})
AUTH_STRIKES = 3
PAGE = 200
CHECKPOINT_EVERY = 20
EMBED_CALL_TEXTS = 128  # unique chunk texts per router.embed call (the router splits into provider batches)
CHUNKER_VERSION = "c1"  # part of the embedding idempotency key: bump when chunking changes


class _Pool:
    """Bounded asyncio worker pool over an async producer. A fatal error or stop request drains the rest."""

    def __init__(self, concurrency: int) -> None:
        self.concurrency = concurrency
        self.stop = asyncio.Event()
        self.stop_reason: str | None = None
        self.stop_detail: str | None = None
        self.fatal: BaseException | None = None
        self.auth_strikes = 0

    def stop_with(self, reason: str, detail: str | None = None) -> None:
        self.stop_reason = self.stop_reason or reason
        self.stop_detail = self.stop_detail or detail
        self.stop.set()

    def fatal_category(self, category: str | None, detail: str | None = None) -> bool:
        """True (and the pool is stopped) when this failure category should end the job."""
        if category == "auth":
            self.auth_strikes += 1
            if self.auth_strikes < AUTH_STRIKES:
                return False
        elif category not in FATAL_CATEGORIES:
            return False
        self.stop_with(str(category), detail)
        return True

    async def run[T](self, produce: AsyncIterator[T], handle: Callable[[T], Awaitable[None]]) -> None:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self.concurrency * 2)

        async def producer() -> None:
            try:
                async for entry in produce:
                    if self.stop.is_set():
                        break
                    await queue.put(entry)
            except Exception as e:  # noqa: BLE001 - surfaced after the workers drain
                self._die(e)
            finally:
                for _ in range(self.concurrency):
                    await queue.put(_END)

        async def worker() -> None:
            while (entry := await queue.get()) is not _END:
                if self.stop.is_set():
                    continue  # drain so the producer can finish
                try:
                    await handle(entry)
                except (JobCancelled, JobLockLost) as e:
                    self._die(e)
                except Exception as e:  # noqa: BLE001
                    log.exception("evidence job worker failed")
                    self._die(e)

        await asyncio.gather(producer(), *(worker() for _ in range(self.concurrency)))
        if self.fatal is not None:
            raise self.fatal

    def _die(self, e: BaseException) -> None:
        self.fatal = self.fatal or e
        self.stop.set()


class _Stats:
    def __init__(self) -> None:
        self.cost = Decimal(0)
        self.input_tokens = self.output_tokens = self.cache_read_tokens = self.cache_write_tokens = 0
        self.documents = self.skipped = self.failed = self.facts = self.chunks = 0
        self.consent_skipped = 0  # documents of residents who denied data_use_ai_processing (ADR-070)
        self.decisions: dict[str, int] = defaultdict(int)
        self.by_route: dict[str, dict[str, Any]] = {}
        self.triggers: dict[str, int] = defaultdict(int)
        self.trigger_documents = self.escalated = self.conflicts_to_review = 0
        self.since_checkpoint = 0

    def add_extraction(self, task: str, outcome: Any) -> None:
        u = outcome.usage
        self.cost += outcome.cost_usd
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cache_read_tokens += u.cache_read_tokens
        self.cache_write_tokens += u.cache_write_tokens
        r = self.by_route.setdefault(task, {"documents": 0, "cost_usd": Decimal(0), "model_ids": set()})
        r["documents"] += 1
        r["cost_usd"] += outcome.cost_usd
        if outcome.model_id:
            r["model_ids"].add(outcome.model_id)

    def snapshot(self, **extra: Any) -> dict[str, Any]:
        return {
            "documents_done": self.documents,
            "skipped_already_processed": self.skipped,
            "skipped_no_ai_consent": self.consent_skipped,
            "failed": self.failed,
            "facts": self.facts,
            "decisions": dict(self.decisions),
            "cost_usd": str(self.cost),
            "tokens": {
                "input": self.input_tokens,
                "output": self.output_tokens,
                "cache_read": self.cache_read_tokens,
                "cache_write": self.cache_write_tokens,
            },
            "by_route": {
                k: {
                    "documents": v["documents"],
                    "cost_usd": str(v["cost_usd"]),
                    "model_ids": sorted(v["model_ids"]),
                }
                for k, v in self.by_route.items()
            },
            "escalation": {
                "escalated": self.escalated,
                "documents_with_triggers": self.trigger_documents,
                "conflicting_facts_sent_to_review": self.conflicts_to_review,
                "triggers": dict(self.triggers),
            },
            **extra,
        }


def _io[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> Awaitable[T]:
    return asyncio.to_thread(partial(fn, *args, **kwargs))


async def _finish_stopped(ctx: JobContext, pool: _Pool, stats: _Stats, **extra: Any) -> None:
    """Persist the final checkpoint; an early stop is recorded as one failed item so the job ends
    `partially_completed` (never `completed`) and `retry` resumes from the idempotent items."""
    snap = stats.snapshot(stopped_reason=pool.stop_reason, stopped_detail=pool.stop_detail, **extra)
    await _io(ctx.checkpoint, snap)
    log.info(
        "evidence run finished: job=%s stopped_reason=%s documents=%s skipped=%s failed=%s facts=%s "
        "cost_usd=%s "
        "tokens=%s routes=%s",
        ctx.job_id, pool.stop_reason, stats.documents, stats.skipped, stats.failed, stats.facts, stats.cost,
        snap["tokens"], snap["by_route"],
    )  # fmt: skip
    if pool.stop_reason:
        item = await _io(ctx.item, f"__stopped__:{pool.stop_reason}")
        if item.status != "failed":
            await _io(item.fail, f"stopped early: {pool.stop_reason}; unprocessed documents remain")
            await _io(ctx.progress, failed=1)


async def _refuse(ctx: JobContext, reason: str, checkpoint: dict[str, Any]) -> None:
    """Record a refusal to start (nothing was sent to any provider); the job ends partially_completed."""
    log.warning("evidence job refused: %s", reason)
    await _io(ctx.checkpoint, {"refused": reason, **checkpoint})
    item = await _io(ctx.item, f"__refused__:{reason[:60]}")
    await _io(item.fail, reason)
    await _io(ctx.progress, failed=1)


def _progress_for(item_status_before: str, *, ok: bool) -> dict[str, int]:
    """Counter deltas that keep processed/succeeded/failed consistent when a failed item is retried."""
    was_failed = item_status_before == "failed"
    if ok:
        return {"processed": 0 if was_failed else 1, "succeeded": 1, "failed": -1 if was_failed else 0}
    return {"processed": 0 if was_failed else 1, "failed": 0 if was_failed else 1}


# ---------------------------------------------------------------------------------------------- shared reads
def _count(
    ctx: JobContext,
    scope: service.DocumentSelector,
    kind: str,
    pending_only: bool,
    residents: Sequence[uuid.UUID] | None,
) -> int:
    with ctx.session() as s:
        return service.count_documents(s, scope, kind=kind, pending_only=pending_only, residents=residents)


def _page(
    ctx: JobContext,
    scope: service.DocumentSelector,
    after: uuid.UUID | None,
    batch: int,
    kind: str,
    pending_only: bool,
    residents: Sequence[uuid.UUID] | None,
) -> list[service.DocRow]:
    with ctx.session() as s:
        return service.select_documents(
            s, scope, after=after, batch=batch, kind=kind, pending_only=pending_only, residents=residents
        )


def _prior_success(ctx: JobContext, keys: list[str]) -> set[str]:
    with ctx.session() as s:
        rows = s.execute(
            select(ProcessingItem.item_key).where(
                ProcessingItem.item_key.in_(keys),
                ProcessingItem.status == "succeeded",
                ProcessingItem.job_id != ctx.job_id,
            )
        )
        return set(rows.scalars())


# ============================================================================================== extraction
def _extraction_key(doc: service.DocRow, task: str, router: Any, prompt_ref: str) -> str:
    primary = router.config.tasks[task].candidates[0].model_id
    return f"{doc.id}:{doc.content_hash[:16]}:{prompt_ref}:{primary}"


def _prior_escalations(ctx: JobContext, model_id: str, prompt_ref: str) -> set[str]:
    """Item keys of every escalation attempted so far (any job, any status): the hard cap spans reruns."""
    with ctx.session() as s:
        rows = s.execute(
            select(ProcessingItem.item_key)
            .where(ProcessingItem.item_key.endswith(f":{prompt_ref}:{model_id}"))
            .distinct()
        )
        return set(rows.scalars())


@register_job("evidence.extract_batch")
def extract_batch(ctx: JobContext) -> None:
    """input_scope: ExtractionScope (cohort_id + document_ids | resident_ids | limit/filters, escalate,
    force)."""
    asyncio.run(_extract(ctx, service.ExtractionScope.model_validate(ctx.job.input_scope)))


async def _extract(ctx: JobContext, scope: service.ExtractionScope) -> None:
    store = service.get_store()
    residents = service.load_cohort_residents(scope, store)  # None only with the audited override
    prior_spend = Decimal(str((ctx.job.checkpoint or {}).get("cost_usd") or "0"))
    router = service.make_router(ctx.session, run_budget=True, initial_run_spend=prior_spend)
    stats, pool = _Stats(), _Pool(scope.concurrency)
    consent_cache: dict[uuid.UUID, bool] = {}  # resident -> denied AI processing (per job)
    pending_only = not scope.force
    try:
        total = await _io(_count, ctx, scope, "extract", pending_only, residents)
        cohort_docs = (
            await _io(_count, ctx, service.DocumentSelector(), "extract", False, residents)
            if residents is not None
            else total
        )
        complex_model = router.config.tasks[COMPLEX].candidates[0].model_id
        used = (
            await _io(_prior_escalations, ctx, complex_model, scope.prompt_ref) if scope.escalate else set()
        )
        share = get_ai_settings().anticipa_ai_escalation_share  # <= 10% by validation; the 10k run uses 2%
        esc = EscalationBudget(int(cohort_docs * share) if scope.escalate else 0, used)
        estimate = estimate_extraction(
            router.config, total, max(esc.cap_total - len(used), 0), run_budget=router.run_budget_usd
        )
        log.info(
            "evidence extraction estimate: job=%s documents=%s cohort_documents=%s escalation_cap=%s "
            "already_escalated=%s expected_usd=%s upper_usd=%s run_budget_usd=%s",
            ctx.job_id, total, cohort_docs, esc.cap_total, len(used),
            estimate["expected_total_usd"], estimate["upper_total_usd"], estimate["run_budget_usd"],
        )  # fmt: skip
        header = {
            "estimate": estimate,
            "cohort_id": scope.cohort_id,
            "override_cohort": scope.override_cohort,
            "escalation_share": str(share),
        }
        await _io(ctx.checkpoint, {**header, **stats.snapshot()})
        await _io(ctx.progress, total=total)

        async def produce() -> AsyncIterator[tuple[service.DocRow, str, bool]]:
            after, selected = None, 0
            while not (scope.limit and selected >= scope.limit):
                batch = min(PAGE, scope.limit - selected) if scope.limit else PAGE
                rows = await _io(_page, ctx, scope, after, batch, "extract", pending_only, residents)
                if not rows:
                    return
                after, selected = rows[-1].id, selected + len(rows)
                keys = [_extraction_key(d, STANDARD, router, scope.prompt_ref) for d in rows]
                done = set() if scope.force else await _io(_prior_success, ctx, keys)
                for doc, key in zip(rows, keys, strict=True):
                    yield doc, key, key in done

        async def handle(entry: tuple[service.DocRow, str, bool]) -> None:
            doc, key, already_done = entry
            await _io(ctx.raise_if_cancelled)
            item = await _io(ctx.item, key)
            if item.done:  # an earlier attempt of THIS job finished it
                stats.skipped += 1
                return
            before = item.status
            if already_done:  # an earlier job processed this exact (document hash, prompt, model)
                await _io(item.skip)
                await _io(ctx.progress, **_progress_for(before, ok=True))
                stats.skipped += 1
                return
            if await _io(ai_processing_denied, ctx.session, doc.resident_id, job_id=ctx.job_id,
                         task="evidence.extract", cache=consent_cache):  # fmt: skip
                await _io(item.skip)  # ADR-070: nothing of this resident goes to a model; a later job retries
                await _io(ctx.progress, **_progress_for(before, ok=True))
                stats.skipped += 1
                stats.consent_skipped += 1
                return
            try:
                doc_text = await _io(store.read_text, doc.storage_key)
            except Exception as e:  # noqa: BLE001 - missing/unreadable file is a per-document failure
                await _fail(ctx, item, doc, before, f"document_unavailable: {type(e).__name__}", stats)
                return
            meta = DocumentMeta(
                document_id=str(doc.id),
                resident_id=str(doc.resident_id),
                document_type=doc.document_type,
                language=doc.language,
                document_date=doc.created_on,
                job_id=str(ctx.job_id),
            )
            outcome = await extract_document(
                router, doc_text, meta, task=STANDARD, prompt_ref=scope.prompt_ref
            )
            stats.add_extraction(STANDARD, outcome)
            if outcome.status != "completed":
                if pool.fatal_category(outcome.error_category, outcome.error_message):
                    return  # item stays pending; nothing persisted
                msg = f"{outcome.error_category}: {outcome.error_message}"
                await _fail(ctx, item, doc, before, msg, stats)  # Haiku already retried transient errors
                return
            pool.auth_strikes = 0
            final = await _maybe_escalate(ctx, router, esc, pool, stats, scope, doc, doc_text, meta, outcome)
            result = await _io(_persist, ctx, doc, final, doc_text)
            await _io(item.succeed)
            await _io(ctx.progress, **_progress_for(before, ok=True))
            stats.documents += 1
            stats.facts += result.facts
            for d in ("accepted", "requires_review", "rejected"):
                stats.decisions[d] += getattr(result, d)
            stats.since_checkpoint += 1
            if stats.since_checkpoint >= CHECKPOINT_EVERY:
                stats.since_checkpoint = 0
                left = (
                    router.run_budget_usd - router.run_spent_usd
                    if router.run_budget_usd is not None
                    else None
                )
                snap = stats.snapshot(escalation_cap=esc.cap_total, run_budget_remaining_usd=str(left))
                await _io(ctx.checkpoint, {**header, **snap})
                log.info(
                    "evidence extraction progress: job=%s documents=%s/%s failed=%s facts=%s tokens_in=%s "
                    "tokens_out=%s cost_usd=%s escalated=%s/%s run_budget_remaining_usd=%s",
                    ctx.job_id, stats.documents + stats.skipped, total, stats.failed, stats.facts,
                    stats.input_tokens + stats.cache_read_tokens + stats.cache_write_tokens,
                    stats.output_tokens,
                    stats.cost, stats.escalated, esc.cap_total, left,
                )  # fmt: skip

        await pool.run(produce(), handle)
        await _finish_stopped(
            ctx, pool, stats, prompt=scope.prompt_ref, total=total, escalation_cap=esc.cap_total,
            escalation_denied_by_cap=esc.denied, **header,
        )  # fmt: skip
    finally:
        await router.aclose()


async def _maybe_escalate(
    ctx: JobContext,
    router: Any,
    esc: EscalationBudget,
    pool: _Pool,
    stats: _Stats,
    scope: service.ExtractionScope,
    doc: service.DocRow,
    doc_text: str,
    meta: DocumentMeta,
    outcome: ExtractionOutcome,
) -> ExtractionOutcome:
    """Re-run on Sonnet when a deterministic trigger fired and the cap has room; otherwise keep the Haiku
    result: its rejected / requires_review facts stay visible, and contradicting family facts that Haiku
    auto-accepted are sent to the clinical review queue. A failed escalation never loses the Haiku result."""
    triggers = escalation_triggers(outcome)
    if not triggers:
        return outcome
    stats.trigger_documents += 1
    for t in triggers:
        stats.triggers[t] += 1
    key = _extraction_key(doc, COMPLEX, router, scope.prompt_ref)
    if scope.escalate and esc.try_take(key):
        item = await _io(ctx.item, key)
        second = await extract_document(router, doc_text, meta, task=COMPLEX, prompt_ref=scope.prompt_ref)
        stats.add_extraction(COMPLEX, second)
        stats.escalated += 1
        if second.status == "completed":
            await _io(item.succeed)
            return second
        await _io(item.fail, f"{second.error_category}: {second.error_message}"[:300])
        pool.fatal_category(second.error_category, second.error_message)
    if CONFLICTING_FAMILY_HISTORY in triggers:
        stats.conflicts_to_review += flag_conflicts_for_review(outcome)
    return outcome


async def _fail(
    ctx: JobContext, item: Any, doc: service.DocRow, before: str, message: str, stats: _Stats
) -> None:
    def mark() -> None:
        with ctx.session() as s:
            service.mark_extraction_failed(s, doc.id)
            s.commit()

    await _io(mark)
    await _io(item.fail, message)
    await _io(ctx.progress, **_progress_for(before, ok=False))
    stats.failed += 1


def _persist(ctx: JobContext, doc: service.DocRow, outcome: Any, doc_text: str) -> service.PersistResult:
    with ctx.session() as s:
        result = service.persist_outcome(s, doc, outcome, doc_text)
        s.commit()
        return result


# =============================================================================================== embeddings
@register_job("evidence.embed_documents")
def embed_documents(ctx: JobContext) -> None:
    """input_scope: EmbeddingScope (cohort_id + selectors, approve_cost_usd). Opt-in: needs
    ANTICIPA_ENABLE_NEBIUS=1
    and an approved cost >= the estimate for the texts that really need embedding."""
    asyncio.run(_embed(ctx, service.EmbeddingScope.model_validate(ctx.job.input_scope)))


class _Plan:
    """What an embedding run has to do, computed without any provider call."""

    def __init__(self) -> None:
        self.docs: dict[uuid.UUID, service.DocRow] = {}
        self.chunks: dict[uuid.UUID, list[Chunk]] = {}
        self.items: dict[uuid.UUID, tuple[Any, str]] = {}  # doc id -> (job item, status before)
        self.waiting: dict[str, list[tuple[uuid.UUID, Chunk]]] = defaultdict(list)  # hash -> rows needing it
        self.texts: dict[str, str] = {}  # unique hash -> text to embed
        # hashes of each document whose chunk rows are not yet WRITTEN. `_upsert_vectors` runs in
        # several threads at
        # once, so every read-modify of this map happens under `lock`; `waiting` is read-only once
        # planning is done.
        self.pending: dict[uuid.UUID, set[str]] = defaultdict(set)
        self.lock = threading.Lock()
        self.reused = 0  # chunk rows filled from an existing embedding (no provider call)
        self.empty_docs = 0
        self.unreadable = 0


def _reuse_existing(s: Any, doc: service.DocRow, chunk: Chunk, model_id: str) -> bool:
    """Fill the chunk row from any stored embedding with the same content hash and model. True when reused."""
    res = s.execute(
        text(
            "INSERT INTO document_chunks (document_id, chunk_index, start_offset, end_offset, content_hash, "
            "embedding, embedding_model, synthetic_status) "
            "SELECT :d, :i, :s, :e, :h, src.embedding, :m, :syn FROM ("
            "SELECT embedding FROM document_chunks WHERE content_hash = :h AND embedding_model = :m "
            "AND embedding IS NOT NULL LIMIT 1) src "
            "ON CONFLICT (document_id, chunk_index) DO UPDATE SET start_offset = excluded.start_offset, "
            "end_offset = excluded.end_offset, content_hash = excluded.content_hash, "
            "embedding = excluded.embedding, embedding_model = excluded.embedding_model"
        ),
        {"d": doc.id, "i": chunk.index, "s": chunk.start, "e": chunk.end, "h": chunk.content_hash,
         "m": model_id, "syn": doc.synthetic_status},
    )  # fmt: skip
    return bool(res.rowcount)


def _plan(
    ctx: JobContext,
    scope: service.EmbeddingScope,
    residents: Sequence[uuid.UUID] | None,
    store: Any,
    model_id: str,
    dims: int,
) -> _Plan:
    plan = _Plan()
    consent_cache: dict[uuid.UUID, bool] = {}
    after, selected = None, 0
    while not (scope.limit and selected >= scope.limit):
        batch = min(PAGE, scope.limit - selected) if scope.limit else PAGE
        rows = _page(ctx, scope, after, batch, "embed", True, residents)
        if not rows:
            break
        after, selected = rows[-1].id, selected + len(rows)
        keys = {d.id: f"{d.id}:{d.content_hash[:16]}:{model_id}:{dims}:{CHUNKER_VERSION}" for d in rows}
        done = _prior_success(ctx, list(keys.values()))
        for doc in rows:
            item = ctx.item(keys[doc.id])
            before = item.status
            if item.done:
                continue
            if keys[doc.id] in done:  # an earlier job embedded exactly this document content with this model
                item.skip()
                ctx.progress(**_progress_for(before, ok=True))
                continue
            if ai_processing_denied(ctx.session, doc.resident_id, job_id=ctx.job_id, task="evidence.embed",
                                    cache=consent_cache):  # fmt: skip
                item.skip()  # ADR-070: the text is not sent to an embedding provider
                ctx.progress(**_progress_for(before, ok=True))
                continue
            try:
                doc_text = store.read_text(doc.storage_key)
            except Exception as e:  # noqa: BLE001
                item.fail(f"document_unavailable: {type(e).__name__}")
                ctx.progress(**_progress_for(before, ok=False))
                plan.unreadable += 1
                continue
            chunks = chunk_text(doc_text)  # blank documents give no chunks: nothing to embed, nothing to pay
            if not chunks:
                plan.empty_docs += 1
                item.succeed()
                ctx.progress(**_progress_for(before, ok=True))
                continue
            plan.docs[doc.id], plan.chunks[doc.id], plan.items[doc.id] = doc, chunks, (item, before)
            with ctx.session() as s:
                have = {
                    (r.chunk_index): (r.content_hash, r.embedded, r.embedding_model)
                    for r in s.execute(
                        select(
                            DocumentChunk.chunk_index,
                            DocumentChunk.content_hash,
                            DocumentChunk.embedding.is_not(None).label("embedded"),
                            DocumentChunk.embedding_model,
                        ).where(DocumentChunk.document_id == doc.id)
                    )
                }
                for c in chunks:
                    if have.get(c.index) == (c.content_hash, True, model_id):
                        continue  # unchanged and already embedded by this model
                    if _reuse_existing(s, doc, c, model_id):
                        plan.reused += 1
                        continue
                    plan.waiting[c.content_hash].append((doc.id, c))
                    plan.pending[doc.id].add(c.content_hash)
                    plan.texts.setdefault(c.content_hash, c.text)  # identical content is embedded once
                s.execute(
                    delete(DocumentChunk).where(
                        DocumentChunk.document_id == doc.id, DocumentChunk.chunk_index >= len(chunks)
                    )
                )
                s.commit()
    return plan


def _upsert_vectors(
    ctx: JobContext, plan: _Plan, vectors: dict[str, list[float]], model_id: str
) -> list[uuid.UUID]:
    """Write the vectors for every chunk row waiting on those hashes; return the documents now complete.

    Runs in worker threads, concurrently for different groups. The DB writes touch only this group's rows
    (`plan.waiting` is read-only here); the shared completion bookkeeping is updated under `plan.lock`
    AFTER the rows
    are committed, so a document is reported complete exactly once and never before all its chunks are
    stored."""
    written: list[tuple[uuid.UUID, str]] = []
    with ctx.session() as s:
        for h, vec in vectors.items():
            for doc_id, c in plan.waiting.get(h, ()):
                doc = plan.docs[doc_id]
                ins = pg_insert(DocumentChunk).values(
                    document_id=doc_id, chunk_index=c.index, start_offset=c.start, end_offset=c.end,
                    content_hash=c.content_hash, embedding=vec, embedding_model=model_id,
                    synthetic_status=doc.synthetic_status,
                )  # fmt: skip
                s.execute(
                    ins.on_conflict_do_update(
                        index_elements=[DocumentChunk.document_id, DocumentChunk.chunk_index],
                        set_={
                            k: ins.excluded[k]
                            for k in (
                                "start_offset",
                                "end_offset",
                                "content_hash",
                                "embedding",
                                "embedding_model",
                            )
                        },
                    )
                )
                written.append((doc_id, c.content_hash))
        s.commit()
    done: list[uuid.UUID] = []
    with plan.lock:
        for doc_id, h in written:
            pending = plan.pending.get(doc_id)
            if pending is not None:
                pending.discard(h)
        for doc_id in {d for d, _ in written}:
            if doc_id in plan.pending and not plan.pending[doc_id]:
                del plan.pending[doc_id]  # removed under the lock: only one caller can ever see it complete
                done.append(doc_id)
    return done


async def _embed(ctx: JobContext, scope: service.EmbeddingScope) -> None:
    settings = get_ai_settings()
    if "nebius" not in settings.enabled_providers:
        await _refuse(ctx, "embeddings are disabled (opt in with ANTICIPA_ENABLE_NEBIUS=1)", {})
        return
    store = service.get_store()
    residents = service.load_cohort_residents(scope, store)
    cfg = load_config()
    route = cfg.tasks["embedding.document"]
    model_id, dims = route.candidates[0].model_id, int(route.dimensions or 0)
    total = await _io(_count, ctx, scope, "embed", True, residents)
    await _io(ctx.progress, total=total)

    plan = await _io(_plan, ctx, scope, residents, store, model_id, dims)  # no provider call so far
    estimate = estimate_embedding(cfg, list(plan.texts.values()))
    est_cost = Decimal(estimate["estimated_cost_usd"])
    approved = (
        scope.approve_cost_usd
        if scope.approve_cost_usd is not None
        else settings.anticipa_nebius_run_approved_usd
    )
    spent = await DbSink(ctx.session).spent_total_usd("nebius")
    credit_left = settings.anticipa_nebius_credit_usd - spent
    info = {
        "estimate": estimate,
        "unique_texts": len(plan.texts),
        "chunk_rows_waiting": sum(len(v) for v in plan.waiting.values()),
        "reused_existing_embeddings": plan.reused,
        "empty_documents": plan.empty_docs,
        "documents_planned": len(plan.docs),
        "approved_usd": str(approved) if approved is not None else None,
        "nebius_credit_usd": str(settings.anticipa_nebius_credit_usd),
        "nebius_spent_usd": str(spent),
        "nebius_remaining_usd": str(credit_left),
        "cohort_id": scope.cohort_id,
    }
    log.info("nebius embedding estimate: job=%s %s", ctx.job_id, info)
    stats, pool = _Stats(), _Pool(scope.concurrency)
    if plan.texts:
        if approved is None or approved < est_cost:
            await _refuse(
                ctx,
                f"cost approval required: estimate ${est_cost} (set approve_cost_usd or "
                "ANTICIPA_NEBIUS_RUN_APPROVED_USD to at least this)",
                info,
            )
            return
        if est_cost > credit_left:
            await _refuse(
                ctx, f"estimate ${est_cost} exceeds the remaining Nebius credit ${credit_left}", info
            )
            return
    router = service.make_router(ctx.session, run_budget=approved if plan.texts else True)
    try:

        async def finish_docs(done_ids: list[uuid.UUID]) -> None:
            for doc_id in done_ids:
                popped = plan.items.pop(doc_id, None)
                if popped is None:  # already finished
                    continue
                item, before = popped
                await _io(item.succeed)
                await _io(ctx.progress, **_progress_for(before, ok=True))
                stats.documents += 1

        # documents whose rows were all filled by reuse / were already current finish without any
        # provider call
        waiting_docs = {d for rows in plan.waiting.values() for d, _ in rows}
        await finish_docs([d for d in list(plan.items) if d not in waiting_docs])
        hashes = list(plan.texts)

        async def produce() -> AsyncIterator[list[str]]:
            for i in range(0, len(hashes), EMBED_CALL_TEXTS):
                yield hashes[i : i + EMBED_CALL_TEXTS]

        async def handle(group: list[str]) -> None:
            await _io(ctx.raise_if_cancelled)
            try:
                res = await router.embed(
                    EmbedRequest(
                        task="embedding.document",
                        texts=[plan.texts[h] for h in group],
                        metadata={"job_id": str(ctx.job_id)},
                    )
                )
            except RouterError as e:
                log.warning("embedding call failed (%s); its documents will be marked failed", e.category)
                pool.fatal_category(e.category, e.message)
                return
            stats.cost += res.cost_usd
            stats.input_tokens += res.usage.input_tokens
            done = await _io(
                _upsert_vectors, ctx, plan, dict(zip(group, res.vectors, strict=True)), res.model_id
            )
            stats.chunks += len(group)
            await finish_docs(done)

        await pool.run(produce(), handle)
        for doc_id in list(plan.items):  # documents that could not be completed (provider error / stopped)
            item, before = plan.items.pop(doc_id)
            if not pool.stop_reason:
                await _io(item.fail, "embedding_failed")
                await _io(ctx.progress, **_progress_for(before, ok=False))
                stats.failed += 1
        remaining = credit_left - stats.cost
        log.info(
            "nebius embedding run: job=%s provider=nebius model=%s tokens=%s documents=%s cost_usd=%s "
            "remaining_credit_usd=%s",
            ctx.job_id, model_id, stats.input_tokens, stats.documents, stats.cost, remaining,
        )  # fmt: skip
        await _finish_stopped(
            ctx,
            pool,
            stats,
            chunks_embedded=stats.chunks,
            total=total,
            remaining_credit_usd=str(remaining),
            **info,
        )
    finally:
        await router.aclose()
