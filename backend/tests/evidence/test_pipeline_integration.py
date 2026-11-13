# ruff: noqa: E501  (long string literals: documents, fixtures, scripted replies)
"""evidence.extract_batch / evidence.embed_documents end to end on the real job machinery and the `anticipa_test`
database, with scripted providers and a fake document store (no network, no keys)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from anticipa.ai.models import ModelInvocation
from anticipa.ai.settings import AiSettings
from anticipa.ai.sinks import DbSink
from anticipa.ai.testing import err, make_test_router
from anticipa.ai.types import AdapterResponse, Usage
from anticipa.consent.models import ConsentRecord
from anticipa.core.models import AuditEvent
from anticipa.core.errors import ValidationFailed
from anticipa.datafabric.models import Document, DocumentChunk
from anticipa.evidence import handlers, service
from anticipa.evidence.cohort import save_manifest, select_cohort
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
HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"


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
    fact("Mother had liver cancer.", "liver cancer"),  # fabricated -> rejected (an escalation trigger)
    fact("Colonoscopy advised next year.", "colonoscopy", category="preventive_care", subject_type="resident",
         relationship=None, temporal_status="planned", extraction_confidence="low"),  # -> requires_review
)  # fmt: skip
CLEAN_NOTE = reply(
    fact("Mother was diagnosed with breast cancer at age 45.", "breast cancer", age_at_onset=45)
)
GOOD_OTHER = reply(
    fact("Brother has type 2 diabetes.", "type 2 diabetes", category="family_history", relationship="brother")
)


class DocAdapter:
    """Provider fake answering by a marker in the document and by model, so it is order-independent under
    concurrency and can play Haiku and Sonnet at once. `by_model` overrides `replies` for one model id."""

    def __init__(self, replies=None, *, by_model=None, i=1500, o=400) -> None:
        self.replies, self.by_model, self.i, self.o = replies or {}, by_model or {}, i, o
        self.calls: list[tuple[str, str]] = []  # (model id, text)
        self.embed_calls: list[list[str]] = []

    async def generate(self, call):
        text = " ".join(m["content"] for m in call.messages)  # repair turns keep the original user message
        self.calls.append((call.model.id, text))
        for marker, out in {**self.replies, **self.by_model.get(call.model.id, {})}.items():
            if marker in text:
                if isinstance(out, Exception):
                    raise out
                return AdapterResponse(text=out, usage=Usage(input_tokens=self.i, output_tokens=self.o))
        return AdapterResponse(text=reply(), usage=Usage(input_tokens=self.i, output_tokens=5))

    def calls_for(self, model_id):
        return [t for m, t in self.calls if m == model_id]

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
        if key not in self.texts:
            raise FileNotFoundError(key)
        return self.texts[key]

    def write_text(self, key: str, text: str) -> str:
        self.texts[key] = text
        return key

    def exists(self, key: str) -> bool:
        return key in self.texts


@pytest.fixture
def store(monkeypatch):
    s = FakeStore()
    monkeypatch.setattr(service, "get_store", lambda: s)
    return s


def install(monkeypatch, adapter, *, nebius=None, budget=None, run_budget=None, spies=None):
    """Patch the router factory: scripted adapters, the real DbSink, per-run/daily budgets as given."""
    adapters = {"anthropic": adapter, **({"nebius": nebius} if nebius else {})}

    def make(sessions, run_budget_arg=True, initial_run_spend=Decimal(0), **kw):
        if spies is not None:
            spies.append(initial_run_spend)
        cap = run_budget_arg if isinstance(run_budget_arg, Decimal) else run_budget
        return make_test_router(
            adapters, sink=DbSink(sessions), daily_budget_usd=budget, run_budget_usd=cap,
            enabled_providers=frozenset({"anthropic", "nebius"} if nebius else {"anthropic"}),
        )[0]  # fmt: skip

    monkeypatch.setattr(
        service,
        "make_router",
        lambda sessions, run_budget=True, initial_run_spend=Decimal(0): make(
            sessions, run_budget, initial_run_spend
        ),
    )


def nebius_on(monkeypatch, **kw):
    monkeypatch.setattr(
        handlers, "get_ai_settings", lambda: AiSettings(_env_file=None, anticipa_enable_nebius=True, **kw)
    )


def add_doc(world, store, rid, text, **kw):
    did = world.document(rid, text, **kw)
    store.texts[f"{world.tag}/{did}.txt"] = text
    return did


def make_cohort(world, sessions, store, size=100):
    with sessions() as s:
        m = select_cohort(s, seed=1, size=size, dataset_version=f"test-{world.tag}")
    save_manifest(store, m)
    return m.cohort_id


def submit(world, sessions, job_type, **scope):
    cls = service.ExtractionScope if job_type.endswith("extract_batch") else service.EmbeddingScope
    with sessions() as s:
        res = jobs.submit_job(s, job_type, cls(**scope).model_dump(mode="json"))
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


def setup(world, store, sessions, docs):
    """One resident per document text; returns (cohort_id, [resident ids], [document ids])."""
    rids, dids = [], []
    for text in docs:
        rid = world.resident(preferred_language="en")
        rids.append(rid)
        dids.append(add_doc(world, store, rid, text))
    return make_cohort(world, sessions, store), rids, dids


# ------------------------------------------------------------------------------------------------ extraction
def test_extraction_persists_every_outcome_with_provenance_on_the_haiku_route(
    world, sessions, store, monkeypatch
):
    cid, (r1, r2), (d1, d2) = setup(world, store, sessions, [NOTE, OTHER])
    adapter = DocAdapter({"Mother was diagnosed": GOOD_NOTE, "Brother has type 2": GOOD_OTHER})
    install(monkeypatch, adapter)

    job = run_job(
        sessions, submit(world, sessions, "evidence.extract_batch", cohort_id=cid, document_ids=[d1, d2])
    )

    assert job.status == "completed"
    assert (job.total_items, job.processed_items, job.succeeded_items, job.failed_items) == (2, 2, 2, 0)
    assert job.checkpoint["facts"] == 5 and job.checkpoint["decisions"] == {
        "accepted": 3,
        "rejected": 1,
        "requires_review": 1,
    }
    assert Decimal(job.checkpoint["cost_usd"]) > 0 and job.checkpoint["tokens"]["input"] == 3000
    assert (
        job.checkpoint["estimate"]["documents"] == 2 and job.checkpoint["cohort_id"] == cid
    )  # estimated before the run
    assert {m for m, _ in adapter.calls} == {
        HAIKU
    }  # Haiku only: a 2-document cohort has an escalation cap of 0
    assert job.checkpoint["escalation"]["escalated"] == 0 and job.checkpoint["escalation_cap"] == 0
    assert (
        job.checkpoint["escalation"]["documents_with_triggers"] == 1
    )  # the NOTE trips triggers but the cap is 0
    facts = {f.fact_type: f for f in facts_of(sessions, r1) + facts_of(sessions, r2)}
    assert set(facts) == {"breast_cancer", "tobacco_use", "liver_cancer", "colonoscopy", "type_2_diabetes"}
    breast = facts["breast_cancer"]
    assert (breast.validation_status, breast.review_status, breast.subject_type, breast.relationship_type) == ("accepted", "not_required", "family_member", "mother")  # fmt: skip
    assert breast.value["age_at_onset"] == 45 and breast.synthetic_status == "synthetic"
    assert (breast.extraction_method, breast.model_provider, breast.model_id, breast.prompt_version) == ("ai_assisted", "anthropic", HAIKU, "evidence.extract@1")  # fmt: skip
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
        assert (
            inv.task == "evidence.extract"
            and inv.job_id == job.id
            and inv.cost_usd > 0
            and inv.provider == "anthropic"
        )
        assert {s.get(Document, d).processing_status for d in (d1, d2)} == {"extracted"}


def test_deterministic_triggers_escalate_at_most_ten_percent_of_the_cohort_to_sonnet(
    world, sessions, store, monkeypatch
):
    # 12 documents: cap = floor(10% x 12) = 1. Three of them trip triggers on Haiku (fabricated quote + low confidence).
    texts = [f"{OTHER} Visit {i}." for i in range(9)] + [f"{NOTE} Visit {i}." for i in range(3)]
    cid, rids, dids = setup(world, store, sessions, texts)
    adapter = DocAdapter(
        {"Brother has type 2": GOOD_OTHER},
        by_model={HAIKU: {"Mother was diagnosed": GOOD_NOTE}, SONNET: {"Mother was diagnosed": CLEAN_NOTE}},
    )
    install(monkeypatch, adapter)

    job = run_job(
        sessions, submit(world, sessions, "evidence.extract_batch", cohort_id=cid, limit=12, concurrency=4)
    )

    assert job.status == "completed" and job.succeeded_items == 12
    esc = job.checkpoint["escalation"]
    assert (
        job.checkpoint["escalation_cap"] == 1
        and esc["escalated"] == 1
        and esc["documents_with_triggers"] == 3
    )
    assert esc["triggers"] == {"unverified_quote": 3, "low_extraction_confidence": 3}
    assert job.checkpoint["escalation_denied_by_cap"] == 2
    assert (
        len(adapter.calls_for(SONNET)) == 1 and len(adapter.calls_for(HAIKU)) == 12
    )  # exactly one Sonnet re-run
    assert set(job.checkpoint["by_route"]) == {"evidence.extract", "evidence.extract.complex"}
    assert job.checkpoint["by_route"]["evidence.extract.complex"]["model_ids"] == [SONNET]
    by_model: dict[str, int] = {}
    for rid in rids[9:]:
        for f in facts_of(sessions, rid):
            by_model.setdefault(f.model_id, 0)
            by_model[f.model_id] += 1
    assert by_model == {
        SONNET: 1,
        HAIKU: 8,
    }  # the Sonnet document holds Sonnet's single clean fact; the others keep Haiku's 4
    with (
        sessions() as s
    ):  # the escalation attempt is recorded as an item (it is what makes the cap span reruns)
        n = s.execute(
            select(func.count())
            .select_from(ProcessingItem)
            .where(
                ProcessingItem.job_id == job.id,
                ProcessingItem.item_key.endswith(f":evidence.extract@1:{SONNET}"),
            )
        ).scalar_one()
    assert n == 1

    again = run_job(sessions, submit(world, sessions, "evidence.extract_batch", cohort_id=cid, limit=12))
    assert (
        len(adapter.calls_for(SONNET)) == 1 and again.checkpoint["skipped_already_processed"] == 0
    )  # nothing pending
    run_job(sessions, submit(world, sessions, "evidence.extract_batch", cohort_id=cid, limit=12, force=True))
    with (
        sessions() as s
    ):  # a forced rerun may revisit the one already-counted document but never take a second slot
        keys = (
            s.execute(
                select(ProcessingItem.item_key)
                .where(
                    ProcessingItem.item_key.endswith(f":evidence.extract@1:{SONNET}"),
                    ProcessingItem.job_id.in_(world.jobs),
                )
                .distinct()
            )
            .scalars()
            .all()
        )
    assert len(keys) == 1


def test_escalation_can_be_switched_off(world, sessions, store, monkeypatch):
    cid, rids, dids = setup(world, store, sessions, [f"{NOTE} Visit {i}." for i in range(10)])
    adapter = DocAdapter({"Mother was diagnosed": GOOD_NOTE})
    install(monkeypatch, adapter)
    job = run_job(
        sessions, submit(world, sessions, "evidence.extract_batch", cohort_id=cid, limit=10, escalate=False)
    )
    assert (
        job.status == "completed"
        and adapter.calls_for(SONNET) == []
        and job.checkpoint["escalation"]["escalated"] == 0
    )


def test_rerun_is_a_noop_and_force_replaces_machine_facts_but_keeps_human_reviewed_ones(
    world, sessions, store, monkeypatch
):
    cid, (rid,), (d1,) = setup(world, store, sessions, [NOTE])
    adapter = DocAdapter({"Mother was diagnosed": GOOD_NOTE})
    install(monkeypatch, adapter)
    run_job(sessions, submit(world, sessions, "evidence.extract_batch", cohort_id=cid, document_ids=[d1]))
    first = {f.id: f for f in facts_of(sessions, rid)}
    assert len(adapter.calls) == 1 and len(first) == 4

    job2 = run_job(
        sessions, submit(world, sessions, "evidence.extract_batch", cohort_id=cid, document_ids=[d1])
    )
    assert len(adapter.calls) == 1 and job2.checkpoint["skipped_already_processed"] == 1  # nothing re-sent
    assert {f.id for f in facts_of(sessions, rid)} == set(first)

    reviewed = next(f for f in first.values() if f.validation_status == "requires_review")
    with sessions() as s:  # a human accepted the queued fact
        f = s.get(EvidenceFact, reviewed.id)
        f.validation_status, f.review_status = "accepted", "reviewed_accepted"
        s.commit()
    job3 = run_job(
        sessions,
        submit(world, sessions, "evidence.extract_batch", cohort_id=cid, document_ids=[d1], force=True),
    )
    assert len(adapter.calls) == 2 and job3.status == "completed"
    after = {f.id: f for f in facts_of(sessions, rid)}
    assert (
        reviewed.id in after and after[reviewed.id].review_status == "reviewed_accepted"
    )  # human decision kept
    assert (
        len(after) == 4 + 1 and len(set(after) - set(first)) == 4
    )  # 4 fresh machine facts + the kept human one


def test_per_document_failures_do_not_stop_the_job_and_a_retry_resumes(world, sessions, store, monkeypatch):
    cid, (rid,), (ok_doc,) = setup(world, store, sessions, [OTHER])
    bad_doc = add_doc(world, store, rid, NOTE)
    missing = world.document(rid, "never stored")  # nothing in the store: file missing
    cid = make_cohort(world, sessions, store)  # a cohort that includes all three documents' resident
    adapter = DocAdapter({"Brother has type 2": GOOD_OTHER, "Mother was diagnosed": "not json at all"})
    install(monkeypatch, adapter)

    job = run_job(
        sessions,
        submit(
            world, sessions, "evidence.extract_batch", cohort_id=cid, document_ids=[ok_doc, bad_doc, missing]
        ),
    )
    assert job.status == "partially_completed"  # never "completed" with failures
    assert (job.total_items, job.processed_items, job.succeeded_items, job.failed_items) == (3, 3, 1, 2)
    with sessions() as s:
        errors = dict(
            s.execute(
                select(ProcessingItem.status, func.count())
                .where(
                    ProcessingItem.job_id == job.id, ProcessingItem.item_key.like("%:evidence.extract@1:%")
                )
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
    assert (
        len(adapter.calls_for(HAIKU)) == 3 and adapter.calls_for(SONNET) == []
    )  # 2 for the bad doc (repair) + 1; no fallback

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


def test_a_transient_haiku_outage_fails_the_document_without_touching_nebius(
    world, sessions, store, monkeypatch
):
    cid, (rid,), (d,) = setup(world, store, sessions, [NOTE])
    adapter = DocAdapter(
        {
            "Mother was diagnosed": err("unavailable"),
        }
    )
    nebius = DocAdapter({"Mother was diagnosed": CLEAN_NOTE})
    install(monkeypatch, adapter, nebius=nebius)
    job = run_job(
        sessions, submit(world, sessions, "evidence.extract_batch", cohort_id=cid, document_ids=[d])
    )
    assert job.status == "partially_completed" and job.failed_items == 1 and facts_of(sessions, rid) == []
    assert nebius.calls == [] and len(adapter.calls) == 3  # Haiku retried (1 + 2), no fallback to Nebius


def test_budget_exceeded_stops_cleanly_without_calling_providers_or_persisting(
    world, sessions, store, monkeypatch
):
    cid, (rid,), _ = setup(world, store, sessions, [f"{NOTE} Visit 0."])
    docs = [add_doc(world, store, rid, f"{NOTE} Visit {i}.") for i in range(1, 4)]
    cid = make_cohort(world, sessions, store)
    adapter = DocAdapter({"Mother was diagnosed": GOOD_NOTE})
    install(monkeypatch, adapter, budget=Decimal("0.0000001"))
    job = run_job(
        sessions,
        submit(world, sessions, "evidence.extract_batch", cohort_id=cid, document_ids=docs, concurrency=2),
    )
    assert job.status == "partially_completed" and adapter.calls == []
    assert job.checkpoint["stopped_reason"] == "budget_exceeded" and job.processed_items == 0
    assert facts_of(sessions, rid) == []
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


def test_the_per_run_budget_stops_a_run_and_a_resume_starts_from_what_was_spent(
    world, sessions, store, monkeypatch
):
    cid, rids, dids = setup(world, store, sessions, [f"{NOTE} Visit {i}." for i in range(6)])
    adapter = DocAdapter({"Mother was diagnosed": CLEAN_NOTE})
    spies: list = []
    install(
        monkeypatch, adapter, run_budget=Decimal("0.0040"), spies=spies
    )  # ~$0.003 worst case per call: room for a few
    job = run_job(
        sessions,
        submit(
            world, sessions, "evidence.extract_batch", cohort_id=cid, limit=6, concurrency=1, escalate=False
        ),
    )
    assert job.status == "partially_completed" and job.checkpoint["stopped_reason"] == "budget_exceeded"
    assert "per-run" in job.checkpoint["stopped_detail"] and 0 < job.processed_items < 6
    spent = Decimal(job.checkpoint["cost_usd"])
    assert 0 < spent <= Decimal("0.0090")
    with sessions() as s:
        jobs.retry_job(s, job.id)
        s.commit()
    run_job(sessions, job.id)
    assert (
        spies[0] == Decimal(0) and spies[1] == spent
    )  # the resumed run is seeded with the earlier attempt's spend


def test_cancellation_is_cooperative(world, sessions, store, monkeypatch):
    cid, _, (d1,) = setup(world, store, sessions, [NOTE])
    adapter = DocAdapter({"Mother was diagnosed": GOOD_NOTE})
    install(monkeypatch, adapter)
    job_id = submit(world, sessions, "evidence.extract_batch", cohort_id=cid, document_ids=[d1])
    with sessions() as s:
        s.get(ProcessingJob, job_id).cancel_requested = True
        s.commit()
    assert run_job(sessions, job_id).status == "cancelled" and adapter.calls == []


def test_cohort_mode_selects_the_next_unprocessed_documents_up_to_the_limit(
    world, sessions, store, monkeypatch
):
    cid, rids, dids = setup(world, store, sessions, [f"{OTHER} Visit {i}." for i in range(5)])
    install(monkeypatch, DocAdapter({"Brother has type 2": GOOD_OTHER}))
    with sessions() as s:
        scope = service.ExtractionScope(cohort_id=cid, limit=3)
        assert service.count_documents(s, scope, residents=rids) == 3
    job = run_job(sessions, submit(world, sessions, "evidence.extract_batch", cohort_id=cid, limit=3))
    assert job.total_items == 3 and job.succeeded_items == 3
    job = run_job(sessions, submit(world, sessions, "evidence.extract_batch", cohort_id=cid, limit=3))
    assert (
        job.total_items == 2 and job.succeeded_items == 2
    )  # only the 2 left: already extracted ones are skipped
    with sessions() as s:
        assert {s.get(Document, d).processing_status for d in dids} == {"extracted"}


def test_documents_outside_the_cohort_are_never_processed_and_a_job_without_a_cohort_refuses(
    world, sessions, store, monkeypatch
):
    cid, (inside,), (d_in,) = setup(world, store, sessions, [OTHER])
    outsider = world.resident()  # created after the manifest: not a member
    d_out = add_doc(world, store, outsider, "Review. Sister has diabetes mellitus.")
    adapter = DocAdapter({"Brother has type 2": GOOD_OTHER})
    install(monkeypatch, adapter)

    job = run_job(
        sessions, submit(world, sessions, "evidence.extract_batch", cohort_id=cid, document_ids=[d_in, d_out])
    )
    assert (
        job.total_items == 1 and adapter.calls_for(HAIKU) != [] and facts_of(sessions, outsider) == []
    )  # the stray id is ignored
    assert len(adapter.calls) == 1

    no_cohort = submit(world, sessions, "evidence.extract_batch", document_ids=[d_in])
    with pytest.raises(ValidationFailed, match="Name an AI cohort"):
        run_job(sessions, no_cohort)

    over = run_job(
        sessions,
        submit(world, sessions, "evidence.extract_batch", override_cohort=True, document_ids=[d_out]),
    )
    assert (
        over.status == "completed" and over.checkpoint["override_cohort"] is True and len(adapter.calls) == 2
    )


# ------------------------------------------------------------------------------------------------ embeddings
LONG = " ".join(f"Sentence {i} about breast screening and follow up." for i in range(60))


def embed_world(world, sessions, store):
    """d_long (many chunks), d_short + d_dup (identical text in two residents), d_blank (no content)."""
    cid, (r1, r2, r3, r4), (d_long, d_short, d_dup, d_blank) = setup(
        world, store, sessions, [LONG, OTHER, OTHER, "   \n  "]
    )
    return cid, (d_long, d_short, d_dup, d_blank)


def chunk_rows(sessions, doc_ids):
    with sessions() as s:
        return list(
            s.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id.in_(doc_ids))
                .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
            ).scalars()
        )


def test_embeddings_are_refused_when_nebius_is_disabled(world, sessions, store, monkeypatch):
    cid, docs = embed_world(world, sessions, store)
    monkeypatch.setattr(handlers, "get_ai_settings", lambda: AiSettings(_env_file=None))  # the default: off
    nebius = DocAdapter()
    install(monkeypatch, DocAdapter(), nebius=nebius)
    job = run_job(
        sessions,
        submit(
            world,
            sessions,
            "evidence.embed_documents",
            cohort_id=cid,
            document_ids=list(docs),
            approve_cost_usd="1",
        ),
    )
    assert job.status == "partially_completed" and "disabled" in job.checkpoint["refused"]
    assert nebius.embed_calls == [] and chunk_rows(sessions, docs) == []


def test_embedding_needs_an_approved_amount_at_least_the_estimate(world, sessions, store, monkeypatch):
    cid, docs = embed_world(world, sessions, store)
    nebius_on(monkeypatch)
    nebius = DocAdapter()
    install(monkeypatch, DocAdapter(), nebius=nebius)
    refused = run_job(
        sessions, submit(world, sessions, "evidence.embed_documents", cohort_id=cid, document_ids=list(docs))
    )
    assert (
        refused.status == "partially_completed" and "cost approval required" in refused.checkpoint["refused"]
    )
    assert (
        refused.checkpoint["estimate"]["estimated_tokens"] > 0 and refused.checkpoint["approved_usd"] is None
    )
    too_low = run_job(
        sessions,
        submit(
            world,
            sessions,
            "evidence.embed_documents",
            cohort_id=cid,
            document_ids=list(docs),
            approve_cost_usd="0.000000",
        ),
    )
    assert "cost approval required" in too_low.checkpoint["refused"]
    assert nebius.embed_calls == [] and chunk_rows(sessions, docs) == []  # nothing was sent to Nebius

    nebius_on(monkeypatch, anticipa_nebius_run_approved_usd="0.01")  # the environment approval also counts
    ok_env = run_job(
        sessions, submit(world, sessions, "evidence.embed_documents", cohort_id=cid, document_ids=list(docs))
    )
    assert ok_env.status == "completed" and len(nebius.embed_calls) >= 1


def test_embedding_job_dedupes_by_content_reuses_stored_vectors_and_logs_the_balance(
    world, sessions, store, monkeypatch
):
    cid, (d_long, d_short, d_dup, d_blank) = embed_world(world, sessions, store)
    nebius_on(monkeypatch)
    nebius = DocAdapter()
    install(monkeypatch, DocAdapter(), nebius=nebius)

    job = run_job(
        sessions,
        submit(
            world,
            sessions,
            "evidence.embed_documents",
            cohort_id=cid,
            document_ids=[d_long, d_short, d_dup, d_blank],
            approve_cost_usd="0.01",
        ),
    )
    assert job.status == "completed" and (job.total_items, job.succeeded_items, job.failed_items) == (4, 4, 0)
    rows = chunk_rows(sessions, [d_long, d_short, d_dup, d_blank])
    by_doc = {d: [c for c in rows if c.document_id == d] for d in (d_long, d_short, d_dup, d_blank)}
    n_long = len(by_doc[d_long])
    assert (
        n_long >= 3 and len(by_doc[d_short]) == 1 and len(by_doc[d_dup]) == 1 and by_doc[d_blank] == []
    )  # blank dropped
    assert all(
        c.embedding is not None
        and len(c.embedding) == 1024
        and c.embedding_model == "Qwen/Qwen3-Embedding-8B"
        for c in rows
    )
    assert by_doc[d_short][0].content_hash == by_doc[d_dup][0].content_hash
    sent = [t for call in nebius.embed_calls for t in call]
    assert len(sent) == n_long + 1 and len(set(sent)) == len(
        sent
    )  # the identical OTHER text was embedded once
    for c in by_doc[d_long]:
        assert LONG[c.start_offset : c.end_offset].strip() == LONG[c.start_offset : c.end_offset]
    cp = job.checkpoint
    assert (
        cp["unique_texts"] == n_long + 1
        and cp["empty_documents"] == 1
        and cp["chunks_embedded"] == n_long + 1
    )
    assert (
        cp["estimate"]["provider"] == "nebius"
        and Decimal(cp["estimate"]["estimated_cost_usd"]) >= Decimal(cp["cost_usd"]) > 0
    )
    assert (
        Decimal(cp["remaining_credit_usd"]) < Decimal("50") and cp["nebius_credit_usd"] == "50.00"
    )  # ledger vs $50
    assert job.checkpoint["stopped_reason"] is None

    n_calls = len(nebius.embed_calls)
    again = run_job(
        sessions,
        submit(
            world,
            sessions,
            "evidence.embed_documents",
            cohort_id=cid,
            document_ids=[d_long, d_short, d_dup, d_blank],
        ),
    )
    assert (
        again.status == "completed" and len(nebius.embed_calls) == n_calls
    )  # unchanged content is never re-embedded

    # new documents with identical content (new residents): the stored vectors are copied, nothing is sent or paid
    third = world.resident(preferred_language="en")
    d_new = add_doc(world, store, third, OTHER)
    cid2 = make_cohort(world, sessions, store)
    reuse = run_job(
        sessions, submit(world, sessions, "evidence.embed_documents", cohort_id=cid2, document_ids=[d_new])
    )
    assert (
        reuse.status == "completed" and len(nebius.embed_calls) == n_calls
    )  # no approval needed: nothing to pay for
    assert reuse.checkpoint["reused_existing_embeddings"] == 1 and reuse.checkpoint["unique_texts"] == 0
    (new_chunk,) = chunk_rows(sessions, [d_new])
    assert new_chunk.embedding is not None and new_chunk.content_hash == by_doc[d_short][0].content_hash


def test_embedding_refuses_when_the_estimate_exceeds_the_remaining_nebius_credit(
    world, sessions, store, monkeypatch
):
    cid, docs = embed_world(world, sessions, store)
    nebius_on(monkeypatch, anticipa_nebius_credit_usd="0")
    nebius = DocAdapter()
    install(monkeypatch, DocAdapter(), nebius=nebius)
    job = run_job(
        sessions,
        submit(
            world,
            sessions,
            "evidence.embed_documents",
            cohort_id=cid,
            document_ids=list(docs),
            approve_cost_usd="1",
        ),
    )
    assert (
        job.status == "partially_completed"
        and "remaining Nebius credit" in job.checkpoint["refused"]
        and nebius.embed_calls == []
    )


def test_embedding_provider_failure_marks_items_failed_without_partial_chunks(
    world, sessions, store, monkeypatch
):
    cid, _, (d,) = setup(world, store, sessions, [OTHER])
    nebius_on(monkeypatch)

    class Failing(DocAdapter):
        async def embed(self, model, texts, dimensions):
            raise err("unavailable", provider="nebius")

    install(monkeypatch, DocAdapter(), nebius=Failing())
    job = run_job(
        sessions,
        submit(
            world, sessions, "evidence.embed_documents", cohort_id=cid, document_ids=[d], approve_cost_usd="1"
        ),
    )
    assert job.status == "partially_completed" and job.failed_items == 1
    assert chunk_rows(sessions, [d]) == []


def test_embedding_job_only_touches_the_cohort(world, sessions, store, monkeypatch):
    cid, _, (d_in,) = setup(world, store, sessions, [OTHER])
    outsider = world.resident()
    d_out = add_doc(world, store, outsider, "Review. Sister has diabetes mellitus.")
    nebius_on(monkeypatch)
    nebius = DocAdapter()
    install(monkeypatch, DocAdapter(), nebius=nebius)
    job = run_job(
        sessions,
        submit(
            world,
            sessions,
            "evidence.embed_documents",
            cohort_id=cid,
            document_ids=[d_in, d_out],
            approve_cost_usd="1",
        ),
    )
    assert (
        job.total_items == 1
        and chunk_rows(sessions, [d_out]) == []
        and len(chunk_rows(sessions, [d_in])) == 1
    )


# ------------------------------------------------------------------------------------------------ consent (ADR-070)
def _ai_consent(sessions, rid, status):
    with sessions() as s:
        s.add(
            ConsentRecord(
                resident_id=rid, scope="data_use_ai_processing", status=status, source="coordinator_entry",
                effective_from=datetime.now(UTC) - timedelta(days=1) + timedelta(seconds=len(status)),
                synthetic_status="simulated",
            )
        )  # fmt: skip
        s.commit()


def _skip_audit(sessions, rid, task):
    with sessions() as s:
        return list(
            s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "consent.ai_processing.skipped", AuditEvent.resource_id == str(rid)
                )
            ).scalars()
        )  # fmt: skip


def test_extraction_skips_a_resident_who_denied_ai_processing_and_a_later_job_picks_them_up(
    world, sessions, store, monkeypatch
):
    cid, (r1, r2), (d1, d2) = setup(world, store, sessions, [NOTE, OTHER])
    _ai_consent(sessions, r2, "deny")
    adapter = DocAdapter({"Mother was diagnosed": GOOD_NOTE, "Brother has type 2": GOOD_OTHER})
    install(monkeypatch, adapter)
    scope = {"cohort_id": cid, "document_ids": [d1, d2]}

    job = run_job(sessions, submit(world, sessions, "evidence.extract_batch", **scope))

    assert job.status == "completed" and (job.total_items, job.processed_items, job.failed_items) == (2, 2, 0)
    assert len(adapter.calls) == 1 and "Brother has type 2" not in adapter.calls[0][1]  # nothing of r2 reached a model
    assert job.checkpoint["skipped_no_ai_consent"] == 1 and facts_of(sessions, r2) == [] and facts_of(sessions, r1)
    with sessions() as s:
        statuses = {i.item_key: i.status for i in s.execute(select(ProcessingItem).where(ProcessingItem.job_id == job.id)).scalars()}
    assert sorted(statuses.values()) == ["skipped", "succeeded"]
    (event,) = _skip_audit(sessions, r2, "evidence.extract")
    assert event.outcome == "denied" and event.details["task"] == "evidence.extract" and event.details["job_id"] == str(job.id)
    assert "Brother" not in str(event.details)  # no document text in the audit trail

    _ai_consent(sessions, r2, "permit")  # opted back in: the next job processes the document
    job2 = run_job(sessions, submit(world, sessions, "evidence.extract_batch", **scope))
    assert job2.checkpoint["skipped_no_ai_consent"] == 0 and len(adapter.calls) == 2 and facts_of(sessions, r2)


def test_embedding_skips_a_resident_who_denied_ai_processing(world, sessions, store, monkeypatch):
    cid, (r1, r2), (d1, d2) = setup(world, store, sessions, [LONG, OTHER])
    _ai_consent(sessions, r2, "deny")
    nebius_on(monkeypatch)
    nebius = DocAdapter()
    install(monkeypatch, DocAdapter(), nebius=nebius)

    job = run_job(
        sessions,
        submit(world, sessions, "evidence.embed_documents", cohort_id=cid, document_ids=[d1, d2], approve_cost_usd="0.01"),
    )

    assert job.status == "completed" and chunk_rows(sessions, [d2]) == [] and chunk_rows(sessions, [d1])
    assert all(OTHER not in t for call in nebius.embed_calls for t in call)  # the text never left the system
    (event,) = _skip_audit(sessions, r2, "evidence.embed")
    assert event.details["task"] == "evidence.embed"
