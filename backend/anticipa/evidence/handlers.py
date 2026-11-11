"""Background jobs of the evidence module (contract 17 §4). Handlers are sync (the worker is threaded); each
runs one asyncio loop for the Model Router and a bounded worker pool.

  evidence.extract_batch      documents -> router (evidence.extract | evidence.extract.complex)
                              -> quote verification -> acceptance policy -> evidence_facts + evidence_sources
  evidence.embed_documents    documents -> offset-preserving chunks -> router.embed (1024-d)
                              -> document_chunks

Both are idempotent per item (document id + content hash + prompt/model), resumable (`ctx.item`), cancellable,
and stop cleanly on `budget_exceeded` / `auth` instead of burning retries. Document text is read from the
document store, never logged, and only the Model Router talks to a provider.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from functools import partial
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from anticipa.ai import EmbedRequest, RouterError
from anticipa.datafabric.models import DocumentChunk
from anticipa.evidence import service
from anticipa.evidence.chunking import Chunk, chunk_text
from anticipa.evidence.extract import PROMPT_REF, DocumentMeta, extract_document
from anticipa.jobs.context import JobCancelled, JobContext, JobLockLost, short_error
from anticipa.jobs.models import ProcessingItem
from anticipa.jobs.registry import register_job

log = logging.getLogger("anticipa.evidence.jobs")
_END = object()
# Retrying the next document would fail the same way. `auth` only counts after AUTH_STRIKES consecutive
# documents: a missing fallback-provider key on one document is not an outage.
FATAL_CATEGORIES = frozenset({"budget_exceeded", "auth"})
AUTH_STRIKES = 3
PAGE = 200
CHECKPOINT_EVERY = 20
EMBED_GROUP = 16  # documents per embedding call
CHUNKER_VERSION = "c1"  # part of the embedding idempotency key: bump when chunking changes


class _Pool:
    """Bounded asyncio worker pool over an async producer. A fatal error or stop request drains the rest."""

    def __init__(self, concurrency: int) -> None:
        self.concurrency = concurrency
        self.stop = asyncio.Event()
        self.stop_reason: str | None = None
        self.fatal: BaseException | None = None
        self.auth_strikes = 0

    def stop_with(self, reason: str) -> None:
        self.stop_reason = self.stop_reason or reason
        self.stop.set()

    def fatal_category(self, category: str | None) -> bool:
        """True (and the pool is stopped) when this failure category should end the job."""
        if category == "auth":
            self.auth_strikes += 1
            if self.auth_strikes < AUTH_STRIKES:
                return False
        elif category not in FATAL_CATEGORIES:
            return False
        self.stop_with(str(category))
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
        self.decisions: dict[str, int] = defaultdict(int)
        self.by_route: dict[str, dict[str, Any]] = {}
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
            **extra,
        }


def _io[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> Awaitable[T]:
    return asyncio.to_thread(partial(fn, *args, **kwargs))


async def _finish_stopped(ctx: JobContext, pool: _Pool, stats: _Stats, **extra: Any) -> None:
    """Persist the final checkpoint; an early stop is recorded as one failed item so the job ends
    `partially_completed` (never `completed`) and `retry` resumes from the idempotent items."""
    await _io(ctx.checkpoint, stats.snapshot(stopped_reason=pool.stop_reason, **extra))
    if pool.stop_reason:
        item = await _io(ctx.item, f"__stopped__:{pool.stop_reason}")
        if item.status != "failed":
            await _io(item.fail, f"stopped early: {pool.stop_reason}; unprocessed documents remain")
            await _io(ctx.progress, failed=1)


def _progress_for(item_status_before: str, *, ok: bool) -> dict[str, int]:
    """Counter deltas that keep processed/succeeded/failed consistent when a failed item is retried."""
    was_failed = item_status_before == "failed"
    if ok:
        return {"processed": 0 if was_failed else 1, "succeeded": 1, "failed": -1 if was_failed else 0}
    return {"processed": 0 if was_failed else 1, "failed": 0 if was_failed else 1}


# ============================================================================================== extraction
def _extraction_key(doc: service.DocRow, task: str, router: Any) -> str:
    primary = router.config.tasks[task].candidates[0].model_id
    return f"{doc.id}:{doc.content_hash[:16]}:{PROMPT_REF}:{primary}"


@register_job("evidence.extract_batch")
def extract_batch(ctx: JobContext) -> None:
    """input_scope: ExtractionScope (document_ids | resident_ids | limit/filters, route, force, ...)."""
    asyncio.run(_extract(ctx, service.ExtractionScope.model_validate(ctx.job.input_scope)))


async def _extract(ctx: JobContext, scope: service.ExtractionScope) -> None:
    router, store = service.make_router(ctx.session), service.get_store()
    stats, pool = _Stats(), _Pool(scope.concurrency)
    pending_only = not scope.force
    try:
        total = await _io(_count, ctx, scope, "extract", pending_only)
        await _io(ctx.progress, total=total)

        async def produce() -> AsyncIterator[tuple[service.DocRow, str, str, bool]]:
            after, selected = None, 0
            while not (scope.limit and selected >= scope.limit):
                batch = min(PAGE, scope.limit - selected) if scope.limit else PAGE
                rows = await _io(_page, ctx, scope, after, batch, "extract", pending_only)
                if not rows:
                    return
                after, selected = rows[-1].id, selected + len(rows)
                entries = []
                for doc in rows:
                    task = service.pick_task(doc.document_type, scope.route)
                    entries.append((doc, task, _extraction_key(doc, task, router)))
                done = set() if scope.force else await _io(_prior_success, ctx, [k for _, _, k in entries])
                for doc, task, key in entries:
                    yield doc, task, key, key in done

        async def handle(entry: tuple[service.DocRow, str, str, bool]) -> None:
            doc, task, key, already_done = entry
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
            try:
                text = await _io(store.read_text, doc.storage_key)
            except Exception as e:  # noqa: BLE001 - missing/unreadable file is a per-document failure
                await _fail(ctx, item, doc, before, f"document_unavailable: {type(e).__name__}", stats)
                return
            outcome = await extract_document(
                router,
                text,
                DocumentMeta(
                    document_id=str(doc.id),
                    resident_id=str(doc.resident_id),
                    document_type=doc.document_type,
                    language=doc.language,
                    document_date=doc.created_on,
                    job_id=str(ctx.job_id),
                ),
                task=task,
            )
            stats.add_extraction(task, outcome)
            if outcome.status == "completed":
                pool.auth_strikes = 0
            else:
                if pool.fatal_category(outcome.error_category):
                    return  # item stays pending; nothing persisted
                msg = f"{outcome.error_category}: {outcome.error_message}"
                await _fail(ctx, item, doc, before, msg, stats)
                return
            result = await _io(_persist, ctx, doc, outcome, text)
            await _io(item.succeed)
            await _io(ctx.progress, **_progress_for(before, ok=True))
            stats.documents += 1
            stats.facts += result.facts
            for d in ("accepted", "requires_review", "rejected"):
                stats.decisions[d] += getattr(result, d)
            stats.since_checkpoint += 1
            if stats.since_checkpoint >= CHECKPOINT_EVERY:
                stats.since_checkpoint = 0
                await _io(ctx.checkpoint, stats.snapshot())

        await pool.run(produce(), handle)
        await _finish_stopped(ctx, pool, stats, prompt=PROMPT_REF, total=total)
    finally:
        await router.aclose()


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


def _count(ctx: JobContext, scope: service.DocumentSelector, kind: str, pending_only: bool) -> int:
    with ctx.session() as s:
        return service.count_documents(s, scope, kind=kind, pending_only=pending_only)


def _page(
    ctx: JobContext,
    scope: service.DocumentSelector,
    after: uuid.UUID | None,
    batch: int,
    kind: str,
    pending_only: bool,
) -> list[service.DocRow]:
    with ctx.session() as s:
        return service.select_documents(
            s, scope, after=after, batch=batch, kind=kind, pending_only=pending_only
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


def _persist(ctx: JobContext, doc: service.DocRow, outcome: Any, text: str) -> service.PersistResult:
    with ctx.session() as s:
        result = service.persist_outcome(s, doc, outcome, text)
        s.commit()
        return result


# =============================================================================================== embeddings
@register_job("evidence.embed_documents")
def embed_documents(ctx: JobContext) -> None:
    """input_scope: EmbeddingScope (document_ids | resident_ids | limit/filters, concurrency)."""
    asyncio.run(_embed(ctx, service.EmbeddingScope.model_validate(ctx.job.input_scope)))


async def _embed(ctx: JobContext, scope: service.EmbeddingScope) -> None:
    router, store = service.make_router(ctx.session), service.get_store()
    route = router.config.tasks["embedding.document"]
    primary = route.candidates[0].model_id
    stats, pool = _Stats(), _Pool(scope.concurrency)
    try:
        total = await _io(_count, ctx, scope, "embed", True)
        await _io(ctx.progress, total=total)

        async def produce() -> AsyncIterator[list[service.DocRow]]:
            after, selected = None, 0
            while not (scope.limit and selected >= scope.limit):
                batch = min(EMBED_GROUP, scope.limit - selected) if scope.limit else EMBED_GROUP
                rows = await _io(_page, ctx, scope, after, batch, "embed", True)
                if not rows:
                    return
                after, selected = rows[-1].id, selected + len(rows)
                yield rows

        async def handle(group: list[service.DocRow]) -> None:
            await _io(ctx.raise_if_cancelled)
            todo: list[tuple[service.DocRow, Any, str, str]] = []  # (doc, item, key, status before)
            for doc in group:
                key = f"{doc.id}:{doc.content_hash[:16]}:{primary}:{route.dimensions}:{CHUNKER_VERSION}"
                item = await _io(ctx.item, key)
                if not item.done:
                    todo.append((doc, item, key, item.status))
            if not todo:
                return
            texts: dict[uuid.UUID, str] = {}
            for doc, item, _, before in list(todo):
                try:
                    texts[doc.id] = await _io(store.read_text, doc.storage_key)
                except Exception as e:  # noqa: BLE001
                    todo = [t for t in todo if t[0].id != doc.id]
                    await _io(item.fail, f"document_unavailable: {type(e).__name__}")
                    await _io(ctx.progress, **_progress_for(before, ok=False))
                    stats.failed += 1
            chunks = {d.id: chunk_text(texts[d.id]) for d, *_ in todo}
            need = await _io(_chunks_needing_embedding, ctx, chunks, primary)
            vectors: dict[tuple[uuid.UUID, int], list[float]] = {}
            model_id = primary
            if need:
                try:
                    res = await router.embed(
                        EmbedRequest(
                            task="embedding.document",
                            texts=[chunks[d][i].text for d, i in need],
                            metadata={"job_id": str(ctx.job_id)},
                        )
                    )
                except RouterError as e:
                    if pool.fatal_category(e.category):
                        return
                    for _, item, _, before in todo:
                        await _io(item.fail, short_error(f"{e.category}: {e.message}"))
                        await _io(ctx.progress, **_progress_for(before, ok=False))
                    stats.failed += len(todo)
                    return
                stats.cost += res.cost_usd
                stats.input_tokens += res.usage.input_tokens
                model_id = res.model_id
                vectors = {k: v for k, v in zip(need, res.vectors, strict=True)}
            await _io(_upsert_chunks, ctx, [d for d, *_ in todo], chunks, vectors, model_id)
            for _, item, _, before in todo:
                await _io(item.succeed)
                await _io(ctx.progress, **_progress_for(before, ok=True))
            stats.documents += len(todo)
            stats.chunks += len(need)

        await pool.run(produce(), handle)
        await _finish_stopped(ctx, pool, stats, chunks_embedded=stats.chunks, total=total)
    finally:
        await router.aclose()


def _chunks_needing_embedding(
    ctx: JobContext, chunks: dict[uuid.UUID, list[Chunk]], model_id: str
) -> list[tuple[uuid.UUID, int]]:
    """(document_id, chunk_index) pairs whose stored chunk is missing, changed or from another model."""
    with ctx.session() as s:
        have = {
            (r.document_id, r.chunk_index): (r.content_hash, r.embedded, r.embedding_model)
            for r in s.execute(
                select(
                    DocumentChunk.document_id,
                    DocumentChunk.chunk_index,
                    DocumentChunk.content_hash,
                    DocumentChunk.embedding.is_not(None).label("embedded"),
                    DocumentChunk.embedding_model,
                ).where(DocumentChunk.document_id.in_(list(chunks)))
            )
        }
    return [
        (doc_id, c.index)
        for doc_id, cs in chunks.items()
        for c in cs
        if have.get((doc_id, c.index)) != (c.content_hash, True, model_id)
    ]


def _upsert_chunks(
    ctx: JobContext,
    docs: list[service.DocRow],
    chunks: dict[uuid.UUID, list[Chunk]],
    vectors: dict[tuple[uuid.UUID, int], list[float]],
    model_id: str,
) -> None:
    with ctx.session() as s:
        for doc in docs:
            cs = chunks[doc.id]
            s.execute(
                delete(DocumentChunk).where(
                    DocumentChunk.document_id == doc.id, DocumentChunk.chunk_index >= len(cs)
                )
            )
            rows = [
                {
                    "document_id": doc.id,
                    "chunk_index": c.index,
                    "start_offset": c.start,
                    "end_offset": c.end,
                    "content_hash": c.content_hash,
                    "embedding": vectors[(doc.id, c.index)],
                    "embedding_model": model_id,
                    "synthetic_status": doc.synthetic_status,
                }
                for c in cs
                if (doc.id, c.index) in vectors
            ]
            if rows:
                ins = pg_insert(DocumentChunk).values(rows)
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
        s.commit()
