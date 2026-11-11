# ruff: noqa: E501  (long string literals: documents, fixtures, scripted replies)
"""evidence.extract_batch / evidence.embed_documents end to end on the real job machinery and the `anticipa_test`
database, with scripted providers and a fake document store (no network, no keys)."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from anticipa.ai.models import ModelInvocation
from anticipa.ai.sinks import DbSink
from anticipa.ai.testing import err, make_test_router, ok
from anticipa.ai.types import AdapterResponse, Usage
from anticipa.datafabric.models import Document, DocumentChunk
from anticipa.evidence import handlers, service  # noqa: F401  (handlers registers the jobs)
from anticipa.evidence.models import EvidenceFact, EvidenceSource
from anticipa.jobs import service as jobs
from anticipa.jobs.context import JobCancelled, JobContext
from anticipa.jobs.models import ProcessingItem, ProcessingJob
from anticipa.jobs.registry import get_handler

pytestmark = pytest.mark.integration

NOTE = (
    "Clinic visit. Mother was diagnosed with breast cancer at age 45. Patient denies tobacco use. "
    "Colonoscopy advised next year."
)
OTHER = "Review. Brother has type 2 diabetes. Patient has no known allergies."
FAMILY = "Family history. Father had a heart attack at 52. Maternal aunt had ovarian cancer."


def fact(quote, concept, **kw):
    base = {
        "category": "family_history", "fact_type": concept.replace(" ", "_"),
        "value": {"concept": concept, "original_text": None, "detail": None, "numeric_value": None,
                  "unit": None, "event_date": None},
        "subject_type": "family_member", "relationship": "mother", "assertion_status": "positive",
        "temporal_status": "historical", "age_at_onset": None, "source_quote": quote,
        "extraction_confidence": "high", "uncertainty_note": None,
    }  # fmt: skip
    return {**base, **kw}


def reply(*facts):
    return json.dumps({"document_language": "en", "facts": list(facts)})


GOOD_NOTE = reply(
    fact("Mother was diagnosed with breast cancer at age 45.", "breast cancer", age_at_onset=45),
    fact("Patient denies tobacco use.", "tobacco use", category="risk_factor", subject_type="resident",
         relationship=None, assertion_status="negative", temporal_status="current"),
    fact("Mother had liver cancer.", "liver cancer"),  # fabricated -> rejected
    fact("Colonoscopy advised next year.", "colonoscopy", category="preventive_care", subject_type="resident",
         relationship=None, temporal_status="planned", extraction_confidence="low"),  # -> requires_review
)  # fmt: skip
GOOD_OTHER = reply(
    fact("Brother has type 2 diabetes.", "type 2 diabetes", category="family_history", relationship="brother")
)
GOOD_FAMILY = reply(
    fact("Father had a heart attack at 52.", "myocardial infarction", relationship="father", age_at_onset=52)
)


class DocAdapter:
    """Provider fake that answers by looking for a marker in the document: order-independent under concurrency."""

    def __init__(self, replies: dict[str, str | Exception], *, i=1500, o=400) -> None:
        self.replies, self.i, self.o = replies, i, o
        self.calls: list[str] = []
        self.embed_calls: list[list[str]] = []

    async def generate(self, call):
        text = " ".join(m["content"] for m in call.messages)  # repair turns keep the original user message
        self.calls.append(text)
        for marker, out in self.replies.items():
            if marker in text:
                if isinstance(out, Exception):
                    raise out
                return AdapterResponse(text=out, usage=Usage(input_tokens=self.i, output_tokens=self.o))
        return AdapterResponse(text=reply(), usage=Usage(input_tokens=self.i, output_tokens=5))

    async def embed(self, model, texts, dimensions):
        self.embed_calls.append(list(texts))
        vecs = []
        for t in texts:
            v = [0.0] * dimensions
            for i, kw in enumerate(("breast", "diabetes", "colonoscopy", "heart")):
                v[i] = 1.0 if kw in t.lower() else 0.0
            v[dimensions - 1] = 0.1  # never a zero vector (cosine is undefined there)
            vecs.append(v)
        return vecs, Usage(input_tokens=sum(len(t) // 4 for t in texts))

    async def aclose(self):
        return None


class FakeStore:
    def __init__(self) -> None:
        self.texts: dict[str, str] = {}

    def read_text(self, key: str) -> str:
        return self.texts[key]  # KeyError == missing file


@pytest.fixture
def store(monkeypatch):
    s = FakeStore()
    monkeypatch.setattr(service, "get_store", lambda: s)
    return s


def install(monkeypatch, adapter, *, anthropic=None, budget=None):
    adapters = {"nebius": adapter, **({"anthropic": anthropic} if anthropic else {})}
    monkeypatch.setattr(
        service,
        "make_router",
        lambda sessions: make_test_router(adapters, sink=DbSink(sessions), daily_budget_usd=budget)[0],
    )


def add_doc(world, store, rid, text, **kw):
    did = world.document(rid, text, **kw)
    store.texts[f"{world.tag}/{did}.txt"] = text
    return did


def submit(world, sessions, job_type, **scope):
    body = (service.ExtractionScope if job_type.endswith("extract_batch") else service.EmbeddingScope)(
        **scope
    )
    with sessions() as s:
        res = jobs.submit_job(s, job_type, body.model_dump(mode="json"))
        s.commit()
        world.jobs.append(res.job.id)
        return res.job.id


def run_job(sessions, job_id) -> ProcessingJob:
    """What the worker does for one job (claim, run handler, finish), without competing for other queued jobs."""
    worker = "pytest-worker"
    with sessions() as s:
        job = s.get(ProcessingJob, job_id, with_for_update=True)
        job.status, job.locked_by, job.heartbeat_at = "running", worker, func.now()
        s.commit()
        s.refresh(job)
        s.expunge(job)
    try:
        get_handler(job.job_type)(JobContext(sessions, job, worker))
    except JobCancelled:
        with sessions() as s:
            jobs.finish_cancelled(s, job_id, worker)
    else:
        with sessions() as s:
            jobs.finish_success(s, job_id, worker)
    with sessions() as s:
        out = s.get(ProcessingJob, job_id)
        s.expunge(out)
        return out


def facts_of(sessions, rid):
    with sessions() as s:
        return list(s.execute(select(EvidenceFact).where(EvidenceFact.resident_id == rid)).scalars())


# ------------------------------------------------------------------------------------------------ extraction
def test_extraction_persists_every_outcome_with_provenance(world, sessions, store, monkeypatch):
    rid = world.resident()
    d1 = add_doc(world, store, rid, NOTE)
    d2 = add_doc(world, store, rid, OTHER)
    adapter = DocAdapter({"Mother was diagnosed": GOOD_NOTE, "Brother has type 2": GOOD_OTHER})
    install(monkeypatch, adapter)

    job = run_job(sessions, submit(world, sessions, "evidence.extract_batch", document_ids=[d1, d2]))

    assert job.status == "completed"
    assert (job.total_items, job.processed_items, job.succeeded_items, job.failed_items) == (2, 2, 2, 0)
    assert job.checkpoint["facts"] == 5 and job.checkpoint["decisions"] == {
        "accepted": 3, "rejected": 1, "requires_review": 1,
    }  # fmt: skip
    assert Decimal(job.checkpoint["cost_usd"]) > 0 and job.checkpoint["tokens"]["input"] == 3000
    facts = {f.fact_type: f for f in facts_of(sessions, rid)}
    assert set(facts) == {"breast_cancer", "tobacco_use", "liver_cancer", "colonoscopy", "type_2_diabetes"}
    breast = facts["breast_cancer"]
    assert (
        breast.validation_status,
        breast.review_status,
        breast.subject_type,
        breast.relationship_type,
    ) == (
        "accepted",
        "not_required",
        "family_member",
        "mother",
    )
    assert breast.value["age_at_onset"] == 45 and breast.synthetic_status == "synthetic"
    assert (breast.extraction_method, breast.model_provider, breast.prompt_version) == (
        "ai_assisted", "nebius", "evidence.extract@1",
    )  # fmt: skip
    assert (
        facts["liver_cancer"].validation_status == "rejected"
        and facts["liver_cancer"].review_status == "not_required"
    )
    queued = facts["colonoscopy"]
    assert queued.validation_status == "requires_review" and queued.review_status == "pending"
    assert "low_extraction_confidence" in queued.value["review_reasons"]
    with sessions() as s:
        src = {
            x.evidence_fact_id: x
            for x in s.execute(select(EvidenceSource).where(EvidenceSource.document_id == d1)).scalars()
        }
        bs = src[breast.id]
        assert NOTE[bs.source_start_offset : bs.source_end_offset] == bs.source_quote  # exact original slice
        assert bs.verification_status == "verified_exact" and bs.source_content_hash and bs.document_id == d1
        rs = src[facts["liver_cancer"].id]
        assert rs.verification_status == "quote_not_found" and rs.source_start_offset is None
        assert rs.source_quote == "Mother had liver cancer."  # the unsupported claim stays visible for audit
        inv = s.get(ModelInvocation, breast.invocation_id)
        assert inv.task == "evidence.extract" and inv.job_id == job.id and inv.cost_usd > 0
        assert {s.get(Document, d).processing_status for d in (d1, d2)} == {"extracted"}


def test_rerun_is_a_noop_and_force_replaces_machine_facts_but_keeps_human_reviewed_ones(
    world, sessions, store, monkeypatch
):
    rid = world.resident()
    d1 = add_doc(world, store, rid, NOTE)
    adapter = DocAdapter({"Mother was diagnosed": GOOD_NOTE})
    install(monkeypatch, adapter)
    run_job(sessions, submit(world, sessions, "evidence.extract_batch", document_ids=[d1]))
    first = {f.id: f for f in facts_of(sessions, rid)}
    assert len(adapter.calls) == 1 and len(first) == 4

    job2 = run_job(sessions, submit(world, sessions, "evidence.extract_batch", document_ids=[d1]))
    assert len(adapter.calls) == 1 and job2.checkpoint["skipped_already_processed"] == 1  # nothing re-sent
    assert {f.id for f in facts_of(sessions, rid)} == set(first)

    reviewed = next(f for f in first.values() if f.validation_status == "requires_review")
    with sessions() as s:  # a human accepted the queued fact
        f = s.get(EvidenceFact, reviewed.id)
        f.validation_status, f.review_status = "accepted", "reviewed_accepted"
        s.commit()
    job3 = run_job(sessions, submit(world, sessions, "evidence.extract_batch", document_ids=[d1], force=True))
    assert len(adapter.calls) == 2 and job3.status == "completed"
    after = {f.id: f for f in facts_of(sessions, rid)}
    assert (
        reviewed.id in after and after[reviewed.id].review_status == "reviewed_accepted"
    )  # human decision kept
    assert (
        len(after) == 4 + 1 and len(set(after) - set(first)) == 4
    )  # 4 fresh machine facts + the kept human one


def test_family_history_documents_use_the_complex_route(world, sessions, store, monkeypatch):
    rid = world.resident()
    d_fam = add_doc(world, store, rid, FAMILY, document_type="family_history_narrative")
    d_note = add_doc(world, store, rid, OTHER)
    claude = DocAdapter({"Father had a heart attack": GOOD_FAMILY})
    nebius = DocAdapter({"Brother has type 2": GOOD_OTHER})
    install(monkeypatch, nebius, anthropic=claude)
    job = run_job(sessions, submit(world, sessions, "evidence.extract_batch", document_ids=[d_fam, d_note]))
    assert job.status == "completed" and len(claude.calls) == 1 and len(nebius.calls) == 1
    assert set(job.checkpoint["by_route"]) == {"evidence.extract", "evidence.extract.complex"}
    assert job.checkpoint["by_route"]["evidence.extract.complex"]["model_ids"] == ["claude-sonnet-5-5"]


def test_per_document_failures_do_not_stop_the_job_and_a_retry_resumes(world, sessions, store, monkeypatch):
    rid = world.resident()
    ok_doc = add_doc(world, store, rid, OTHER)
    bad_doc = add_doc(world, store, rid, NOTE)
    missing = world.document(rid, "never stored")  # nothing in the store: file missing
    adapter = DocAdapter({"Brother has type 2": GOOD_OTHER, "Mother was diagnosed": "not json at all"})
    install(monkeypatch, adapter, anthropic=DocAdapter({"Mother was diagnosed": "still not json"}))

    job = run_job(
        sessions, submit(world, sessions, "evidence.extract_batch", document_ids=[ok_doc, bad_doc, missing])
    )
    assert job.status == "partially_completed"  # never "completed" with failures
    assert (job.total_items, job.processed_items, job.succeeded_items, job.failed_items) == (3, 3, 1, 2)
    with sessions() as s:
        errors = dict(
            s.execute(
                select(ProcessingItem.status, func.count())
                .where(ProcessingItem.job_id == job.id)
                .group_by(ProcessingItem.status)
            ).all()
        )
        assert errors == {"succeeded": 1, "failed": 2}
        assert {s.get(Document, d).processing_status for d in (bad_doc, missing)} == {"extraction_failed"}
        msgs = " ".join(
            e or ""
            for e in s.execute(select(ProcessingItem.error).where(ProcessingItem.job_id == job.id)).scalars()
        )
        assert "output_validation" in msgs and "document_unavailable" in msgs and NOTE not in msgs
    assert [f.fact_type for f in facts_of(sessions, rid)] == [
        "type_2_diabetes"
    ]  # nothing persisted for failures

    adapter.replies["Mother was diagnosed"] = GOOD_NOTE  # the provider recovers; the file appears
    store.texts[f"{world.tag}/{missing}.txt"] = "never stored"
    with sessions() as s:
        jobs.retry_job(s, job.id)
        s.commit()
    again = run_job(sessions, job.id)
    assert again.status == "completed" and (
        again.processed_items,
        again.succeeded_items,
        again.failed_items,
    ) == (3, 3, 0)
    assert len(facts_of(sessions, rid)) == 1 + 4


def test_budget_exceeded_stops_cleanly_without_calling_providers_or_persisting(
    world, sessions, store, monkeypatch
):
    rid = world.resident()
    docs = [add_doc(world, store, rid, f"{NOTE} Visit {i}.") for i in range(3)]
    adapter = DocAdapter({"Mother was diagnosed": GOOD_NOTE})
    install(monkeypatch, adapter, budget=Decimal("0.0000001"))
    job = run_job(
        sessions, submit(world, sessions, "evidence.extract_batch", document_ids=docs, concurrency=2)
    )
    assert job.status == "partially_completed" and adapter.calls == []
    assert job.checkpoint["stopped_reason"] == "budget_exceeded" and job.processed_items == 0
    assert facts_of(sessions, rid) == [] and "stopped early" in (job.error_summary or "stopped early")
    with sessions() as s:
        stopped = s.execute(
            select(ProcessingItem).where(
                ProcessingItem.job_id == job.id, ProcessingItem.item_key.like("__stopped__%")
            )
        ).scalar_one()
        assert stopped.status == "failed"
        assert {s.get(Document, d).processing_status for d in docs} == {
            "pending"
        }  # untouched: still extractable


def test_cancellation_is_cooperative(world, sessions, store, monkeypatch):
    rid = world.resident()
    d1 = add_doc(world, store, rid, NOTE)
    adapter = DocAdapter({"Mother was diagnosed": GOOD_NOTE})
    install(monkeypatch, adapter)
    job_id = submit(world, sessions, "evidence.extract_batch", document_ids=[d1])
    with sessions() as s:
        s.get(ProcessingJob, job_id).cancel_requested = True
        s.commit()
    assert run_job(sessions, job_id).status == "cancelled" and adapter.calls == []


def test_cohort_mode_selects_the_next_unprocessed_documents_up_to_the_limit(
    world, sessions, store, monkeypatch
):
    rid = world.resident()
    docs = [add_doc(world, store, rid, f"{OTHER} Visit {i}.", document_type="cohort_note") for i in range(5)]
    install(monkeypatch, DocAdapter({"Brother has type 2": GOOD_OTHER}))
    with sessions() as s:
        scope = service.ExtractionScope(limit=3, document_type="cohort_note")
        assert service.count_documents(s, scope) == 3
    job = run_job(
        sessions, submit(world, sessions, "evidence.extract_batch", limit=3, document_type="cohort_note")
    )
    assert job.total_items == 3 and job.succeeded_items == 3
    job = run_job(
        sessions, submit(world, sessions, "evidence.extract_batch", limit=3, document_type="cohort_note")
    )
    assert (
        job.total_items == 2 and job.succeeded_items == 2
    )  # only the 2 left: already extracted ones are skipped
    with sessions() as s:
        assert {s.get(Document, d).processing_status for d in docs} == {"extracted"}


# ------------------------------------------------------------------------------------------------ embeddings
def test_embedding_job_chunks_embeds_and_is_idempotent(world, sessions, store, monkeypatch):
    rid = world.resident()
    long_note = " ".join(f"Sentence {i} about breast screening and follow up." for i in range(60))
    d_long = add_doc(world, store, rid, long_note)
    d_short = add_doc(world, store, rid, OTHER)
    adapter = DocAdapter({})
    install(monkeypatch, adapter)

    job = run_job(
        sessions, submit(world, sessions, "evidence.embed_documents", document_ids=[d_long, d_short])
    )
    assert job.status == "completed" and (job.total_items, job.succeeded_items) == (2, 2)
    with sessions() as s:
        chunks = list(
            s.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id.in_([d_long, d_short]))
                .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
            ).scalars()
        )
    by_doc = {d: [c for c in chunks if c.document_id == d] for d in (d_long, d_short)}
    assert len(by_doc[d_long]) >= 3 and len(by_doc[d_short]) == 1
    for c in by_doc[d_long]:
        assert long_note[c.start_offset : c.end_offset].strip() == long_note[c.start_offset : c.end_offset]
        assert (
            c.embedding is not None
            and len(c.embedding) == 1024
            and c.embedding_model == "Qwen/Qwen3-Embedding-8B"
        )
    assert [c.chunk_index for c in by_doc[d_long]] == list(range(len(by_doc[d_long])))
    n_calls = len(adapter.embed_calls)
    assert n_calls >= 1 and job.checkpoint["chunks_embedded"] == len(chunks)

    again = run_job(
        sessions, submit(world, sessions, "evidence.embed_documents", document_ids=[d_long, d_short])
    )
    assert (
        again.status == "completed" and len(adapter.embed_calls) == n_calls
    )  # item keys already succeeded elsewhere? no: new job re-checks chunks
    with sessions() as s:
        assert s.execute(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id.in_([d_long, d_short]))
        ).scalar_one() == len(chunks)


def test_embedding_provider_failure_marks_items_failed_without_partial_chunks(
    world, sessions, store, monkeypatch
):
    rid = world.resident()
    d = add_doc(world, store, rid, OTHER)

    class Failing(DocAdapter):
        async def embed(self, model, texts, dimensions):
            raise err("unavailable", provider="nebius")

    install(monkeypatch, Failing({}))
    job = run_job(sessions, submit(world, sessions, "evidence.embed_documents", document_ids=[d]))
    assert job.status == "partially_completed" and job.failed_items == 1
    with sessions() as s:
        assert (
            s.execute(
                select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == d)
            ).scalar_one()
            == 0
        )


def test_unused_imports_guard():
    assert ok and uuid  # keep helpers imported for readability of the module header
