# ruff: noqa: E501
"""Regression: `_upsert_vectors` runs in several threads at once (the pool hands each embedding group to
`asyncio.to_thread`). It used to pop from `plan.waiting` while another thread iterated `plan.waiting.values()`, which
failed a real run with "RuntimeError: dictionary changed size during iteration". No database needed."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from anticipa.evidence import handlers, service
from anticipa.evidence.chunking import chunk_text

MODEL = "Qwen/Qwen3-Embedding-8B"


class FakeSession:
    """Accepts any statement; sleeps briefly so the threads really overlap."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_a, **_k):
        time.sleep(0.0002)

    def commit(self):
        time.sleep(0.0002)


class FakeCtx:
    def session(self) -> FakeSession:
        return FakeSession()


def build_plan(n_docs: int, chunks_per_doc: int) -> handlers._Plan:
    plan = handlers._Plan()
    for d in range(n_docs):
        doc = service.DocRow(uuid.uuid4(), uuid.uuid4(), "clinical_note", "en", f"k{d}", f"h{d}", None, "synthetic")
        text = " ".join(f"Document {d} sentence {i} about screening follow up." for i in range(chunks_per_doc * 30))
        chunks = chunk_text(text, target_chars=300, max_chars=600)[:chunks_per_doc]
        plan.docs[doc.id], plan.chunks[doc.id], plan.items[doc.id] = doc, chunks, (object(), "pending")
        for c in chunks:
            plan.waiting[c.content_hash].append((doc.id, c))
            plan.pending[doc.id].add(c.content_hash)
            plan.texts.setdefault(c.content_hash, c.text)
    return plan


def test_concurrent_upserts_complete_every_document_exactly_once_and_never_raise():
    plan = build_plan(n_docs=150, chunks_per_doc=4)
    hashes = list(plan.texts)
    groups = [hashes[i : i + 7] for i in range(0, len(hashes), 7)]  # small groups: lots of interleaving
    barrier = threading.Barrier(8)

    def run(group: list[str]) -> list[uuid.UUID]:
        try:
            barrier.wait(timeout=0.5)  # start together on the first wave
        except threading.BrokenBarrierError:
            pass
        return handlers._upsert_vectors(FakeCtx(), plan, dict.fromkeys(group, [0.1] * 4), MODEL)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, groups))  # re-raises any exception from a worker thread

    done = [d for r in results for d in r]
    assert len(done) == len(set(done)) == 150  # each document reported complete once, none lost
    assert set(done) == set(plan.docs) and not plan.pending  # nothing left pending
    assert sum(len(v) for v in plan.waiting.values()) == sum(len(c) for c in plan.chunks.values())  # waiting untouched


def test_a_document_is_not_complete_until_every_group_it_needs_has_been_written():
    plan = build_plan(n_docs=1, chunks_per_doc=3)
    (doc_id,) = plan.docs
    first, second, third = (plan.chunks[doc_id][i].content_hash for i in range(3))
    ctx = FakeCtx()
    assert handlers._upsert_vectors(ctx, plan, {first: [0.0]}, MODEL) == []
    assert handlers._upsert_vectors(ctx, plan, {second: [0.0]}, MODEL) == []
    assert handlers._upsert_vectors(ctx, plan, {third: [0.0]}, MODEL) == [doc_id]
    assert handlers._upsert_vectors(ctx, plan, {third: [0.0]}, MODEL) == []  # a repeat never completes it twice
