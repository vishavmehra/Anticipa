# ruff: noqa: E501  (long string literals: documents, fixtures, scripted replies)
"""Evidence API: lists, review queue, detail, review, job submission and search, with roles (docs/17 §2, ADR-060)."""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from anticipa.ai.testing import FakeAdapter, err, make_test_router, ok
from anticipa.ai.types import AdapterResponse, Usage
from anticipa.api.errors import install_exception_handlers
from anticipa.core.auth import CurrentUser, get_current_user
from anticipa.core.models import AuditEvent
from anticipa.datafabric.models import DocumentChunk
from anticipa.evidence import DocumentMeta, extract_document, service
from anticipa.evidence.models import EvidenceFact, EvidenceSource
from anticipa.evidence.routes import router as evidence_router

pytestmark = pytest.mark.integration

TEXT = (
    "Clinic visit. Mother was diagnosed with breast cancer at age 45. Patient denies tobacco use. "
    "Colonoscopy advised next year."
)
VALUE = {
    "concept": "x",
    "original_text": None,
    "detail": None,
    "numeric_value": None,
    "unit": None,
    "event_date": None,
}


def cand(quote, concept, **kw):
    base = {
        "category": "family_history", "fact_type": concept.replace(" ", "_"), "value": {**VALUE, "concept": concept},
        "subject_type": "family_member", "relationship": "mother", "assertion_status": "positive",
        "temporal_status": "historical", "age_at_onset": None, "source_quote": quote,
        "extraction_confidence": "high", "uncertainty_note": None,
    }  # fmt: skip
    return {**base, **kw}


REPLY = json.dumps(
    {
        "document_language": "en",
        "facts": [
            cand("Mother was diagnosed with breast cancer at age 45.", "breast cancer", age_at_onset=45),  # accepted
            cand("Mother had liver cancer.", "liver cancer"),  # rejected: quote not in the document
            cand("Colonoscopy advised next year.", "colonoscopy", category="preventive_care", subject_type="resident",
                 relationship=None, temporal_status="planned", extraction_confidence="low"),  # requires_review
        ],
    }
)  # fmt: skip


def user(role: str) -> CurrentUser:
    return CurrentUser(id=uuid.uuid4(), email=f"{role}@example.test", role=role)


@pytest.fixture
def api(sessions, monkeypatch):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(evidence_router, prefix="/api/v1")
    app.state.sessionmaker = sessions
    holder = {"user": user("clinical_reviewer")}
    app.dependency_overrides[get_current_user] = lambda: holder["user"]
    client = TestClient(app, raise_server_exceptions=False)
    client.as_role = lambda role: holder.__setitem__("user", user(role))  # type: ignore[attr-defined]
    client.state = app.state  # type: ignore[attr-defined]
    return client


class Store:
    def __init__(self, texts):
        self.texts = texts

    def read_text(self, key):
        return self.texts[key]


@pytest.fixture
def seeded(world, sessions, monkeypatch):
    """One resident, one document, three extracted facts (accepted / rejected / requires_review)."""
    rid = world.resident()
    did = world.document(rid, TEXT)
    key = f"{world.tag}/{did}.txt"
    monkeypatch.setattr(service, "get_store", lambda: Store({key: TEXT}))
    router, _, _ = make_test_router({"nebius": FakeAdapter(ok(REPLY))})
    outcome = asyncio.run(extract_document(router, TEXT, DocumentMeta(document_id=str(did))))
    doc = service.DocRow(did, rid, "clinical_note", "en", key, "h", None, "synthetic")
    with sessions() as s:
        service.persist_outcome(s, doc, outcome, TEXT)
        s.commit()
        facts = {
            f.fact_type: f.id
            for f in s.execute(select(EvidenceFact).where(EvidenceFact.resident_id == rid)).scalars()
        }
    return {"resident": rid, "document": did, "facts": facts, "key": key}


# ----------------------------------------------------------------------------------------------- reads
def test_review_queue_lists_only_requires_review_with_sources_and_pagination(api, seeded):
    r = api.get(
        "/api/v1/evidence",
        params={"validation_status": "requires_review", "resident_id": str(seeded["resident"])},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["total"] == 1 and body["meta"]["page"] == 1 and body["meta"]["total_pages"] == 1
    fact = body["data"][0]
    assert fact["fact_type"] == "colonoscopy" and fact["review_status"] == "pending"
    assert fact["review_reasons"] == ["low_extraction_confidence"] and "review_reasons" not in fact["value"]
    src = fact["sources"][0]
    assert TEXT[src["start_offset"] : src["end_offset"]] == src["quote"] == "Colonoscopy advised next year."
    assert src["verification_status"] == "verified_exact" and src["document_id"] == str(seeded["document"])
    assert fact["synthetic_status"] == "synthetic" and fact["model_provider"] == "nebius"

    page = api.get(
        "/api/v1/evidence", params={"resident_id": str(seeded["resident"]), "page_size": 2, "page": 2}
    ).json()
    assert page["meta"]["total"] == 3 and page["meta"]["total_pages"] == 2 and len(page["data"]) == 1
    assert api.get("/api/v1/evidence", params={"validation_status": "bogus"}).status_code == 422


def test_resident_evidence_filters_and_unknown_resident(api, seeded):
    rid = seeded["resident"]
    r = api.get(
        f"/api/v1/residents/{rid}/evidence",
        params={"category": "family_history", "assertion_status": "positive"},
    )
    assert r.status_code == 200 and {f["fact_type"] for f in r.json()["data"]} == {
        "breast_cancer",
        "liver_cancer",
    }
    r = api.get(
        f"/api/v1/residents/{rid}/evidence",
        params={"document_id": str(seeded["document"]), "validation_status": "rejected"},
    )
    rejected = r.json()["data"]
    assert [f["fact_type"] for f in rejected] == ["liver_cancer"]
    assert (
        rejected[0]["sources"][0]["verification_status"] == "quote_not_found"
    )  # visible, never silently dropped
    assert rejected[0]["sources"][0]["start_offset"] is None
    assert api.get(f"/api/v1/residents/{uuid.uuid4()}/evidence").status_code == 404


def test_detail_has_sources_context_and_no_review_yet(api, seeded):
    fid = seeded["facts"]["breast_cancer"]
    r = api.get(f"/api/v1/evidence/{fid}", params={"context": 12})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["validation_status"] == "accepted" and d["value"]["age_at_onset"] == 45 and d["review"] is None
    assert d["document"]["document_type"] == "clinical_note" and d["prompt_version"] == "evidence.extract@1"
    ctx = d["context"][0]
    assert ctx["quote"] == "Mother was diagnosed with breast cancer at age 45."
    assert ctx["before"] == TEXT[: TEXT.index(ctx["quote"])][-12:] and ctx["after"] == " Patient den"
    assert api.get(f"/api/v1/evidence/{uuid.uuid4()}").status_code == 404
    assert api.get("/api/v1/evidence/not-a-uuid").status_code == 422


# ---------------------------------------------------------------------------------------------- review
def test_review_accept_is_audited_idempotent_and_final(api, seeded, sessions):
    fid = seeded["facts"]["colonoscopy"]
    r = api.post(
        f"/api/v1/evidence/{fid}/review", json={"decision": "accepted", "review_note": "matches the source"}
    )
    assert r.status_code == 200
    out = r.json()["data"]
    assert (out["validation_status"], out["review_status"], out["previous_validation_status"], out["changed"]) == (
        "accepted", "reviewed_accepted", "requires_review", True,
    )  # fmt: skip
    again = api.post(f"/api/v1/evidence/{fid}/review", json={"decision": "accepted"}).json()["data"]
    assert again["changed"] is False
    conflict = api.post(f"/api/v1/evidence/{fid}/review", json={"decision": "rejected"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "INVALID_TRANSITION"
    with sessions() as s:
        events = list(s.execute(select(AuditEvent).where(AuditEvent.resource_id == str(fid))).scalars())
        assert (
            len(events) == 1
            and events[0].action == "evidence.review"
            and events[0].actor_role == "clinical_reviewer"
        )
        assert (
            events[0].details["decision"] == "accepted"
            and events[0].details["review_note"] == "matches the source"
        )
        fact = s.get(EvidenceFact, fid)
        assert (
            fact.extraction_confidence == "low" and fact.value["concept"] == "colonoscopy"
        )  # machine output intact
    detail = api.get(f"/api/v1/evidence/{fid}").json()["data"]
    assert detail["review"]["decision"] == "accepted" and detail["review"]["note"] == "matches the source"
    assert (
        detail["review"]["reviewer_role"] == "clinical_reviewer" and detail["review"]["reviewer_id"] is None
    )
    queue = api.get(
        "/api/v1/evidence",
        params={"validation_status": "requires_review", "resident_id": str(seeded["resident"])},
    )
    assert queue.json()["meta"]["total"] == 0


def test_review_reject_and_the_states_a_reviewer_cannot_touch(api, seeded, sessions):
    f = seeded["facts"]
    assert (
        api.post(f"/api/v1/evidence/{f['colonoscopy']}/review", json={"decision": "rejected"}).json()["data"][
            "validation_status"
        ]
        == "rejected"
    )
    for name in (
        "liver_cancer",
        "breast_cancer",
    ):  # auto-rejected (quote not found) and auto-accepted: not in the queue
        r = api.post(f"/api/v1/evidence/{f[name]}/review", json={"decision": "accepted"})
        assert r.status_code == 409, name
    assert (
        api.post(f"/api/v1/evidence/{uuid.uuid4()}/review", json={"decision": "accepted"}).status_code == 404
    )
    assert (
        api.post(f"/api/v1/evidence/{f['colonoscopy']}/review", json={"decision": "maybe"}).status_code == 422
    )


def test_unverified_source_can_never_be_human_accepted(api, seeded, sessions):
    fid = seeded["facts"]["colonoscopy"]
    with sessions() as s:
        s.execute(
            update(EvidenceSource)
            .where(EvidenceSource.evidence_fact_id == fid)
            .values(verification_status="span_mismatch")
        )
        s.commit()
    r = api.post(f"/api/v1/evidence/{fid}/review", json={"decision": "accepted"})
    assert r.status_code == 409 and "not verified" in r.json()["error"]["message"]
    assert (
        api.post(f"/api/v1/evidence/{fid}/review", json={"decision": "rejected"}).status_code == 200
    )  # rejecting is fine


# ----------------------------------------------------------------------------------------------- roles
@pytest.mark.parametrize("role", ["observer", "care_coordinator", "population_admin", "system_admin"])
def test_only_clinical_reviewers_review(api, seeded, role):
    api.as_role(role)
    r = api.post(f"/api/v1/evidence/{seeded['facts']['colonoscopy']}/review", json={"decision": "accepted"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


def test_observer_reads_everything_but_triggers_nothing(api, seeded):
    api.as_role("observer")
    fid = seeded["facts"]["breast_cancer"]
    assert api.get("/api/v1/evidence").status_code == 200
    assert api.get(f"/api/v1/evidence/{fid}").status_code == 200
    assert api.get(f"/api/v1/residents/{seeded['resident']}/evidence").status_code == 200
    for path in ("/api/v1/evidence/extraction-jobs", "/api/v1/evidence/embedding-jobs"):
        assert api.post(path, json={"limit": 1}).status_code == 403
    assert (
        api.get("/api/v1/evidence/search", params={"q": "breast"}).status_code == 403
    )  # search calls a paid provider
    api.as_role("care_coordinator")  # no evidence.view at all
    assert api.get("/api/v1/evidence").status_code == 403


# ------------------------------------------------------------------------------------------------- jobs
def test_extraction_job_submission_validation_and_idempotency(api, seeded, world):
    api.as_role("population_admin")
    body = {"resident_ids": [str(seeded["resident"])], "idempotency_key": f"k-{world.tag}", "route": "auto"}
    r = api.post("/api/v1/evidence/extraction-jobs", json=body)
    assert r.status_code == 202
    first = r.json()["data"]
    world.jobs.append(uuid.UUID(first["job_id"]))
    assert first["job_type"] == "evidence.extract_batch" and first["created"] and first["document_count"] == 1
    again = api.post("/api/v1/evidence/extraction-jobs", json=body)
    assert (
        again.status_code == 200
        and again.json()["data"]["job_id"] == first["job_id"]
        and not again.json()["data"]["created"]
    )
    assert (
        api.post("/api/v1/evidence/extraction-jobs", json={}).status_code == 422
    )  # no selection: never "everything"
    assert api.post("/api/v1/evidence/extraction-jobs", json={"limit": 5, "surprise": 1}).status_code == 422
    assert api.post("/api/v1/evidence/extraction-jobs", json={"limit": 0}).status_code == 422
    assert (
        api.post("/api/v1/evidence/extraction-jobs", json={"limit": 10, "concurrency": 99}).status_code == 422
    )


def test_embedding_job_submission(api, seeded, world):
    api.as_role("system_admin")
    r = api.post("/api/v1/evidence/embedding-jobs", json={"document_ids": [str(seeded["document"])]})
    assert r.status_code == 202
    world.jobs.append(uuid.UUID(r.json()["data"]["job_id"]))
    assert (
        r.json()["data"]["job_type"] == "evidence.embed_documents" and r.json()["data"]["document_count"] == 1
    )


# ----------------------------------------------------------------------------------------------- search
class EmbedAdapter(FakeAdapter):
    async def embed(self, model, texts, dimensions):
        out = []
        for t in texts:
            v = [0.0] * dimensions
            v[0] = 1.0 if "breast" in t.lower() else 0.0
            v[1] = 1.0 if "colonoscopy" in t.lower() else 0.0
            v[dimensions - 1] = 0.1
            out.append(v)
        return out, Usage(input_tokens=5)


def test_search_is_cosine_ranked_scoped_labelled_and_budget_guarded(api, seeded, sessions):
    def vec(x0, x1):
        v = [0.0] * 1024
        v[0], v[1], v[1023] = x0, x1, 0.1
        return v

    a, b = TEXT.index("Mother"), TEXT.index("Colonoscopy")
    with sessions() as s:
        s.add_all([
            DocumentChunk(document_id=seeded["document"], chunk_index=0, start_offset=a, end_offset=b - 1, content_hash="a",
                          embedding=vec(1, 0), embedding_model="m"),
            DocumentChunk(document_id=seeded["document"], chunk_index=1, start_offset=b, end_offset=len(TEXT), content_hash="b",
                          embedding=vec(0, 1), embedding_model="m"),
        ])  # fmt: skip
        s.commit()
    router, sink, _ = make_test_router({"nebius": EmbedAdapter()})
    api.state.ai_router = router

    r = api.get(
        "/api/v1/evidence/search",
        params={"q": "breast cancer family", "resident_id": str(seeded["resident"])},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert (
        data["retrieval_only"] is True
        and "never establish" in data["notice"]
        and data["query_model"].startswith("Qwen")
    )
    hits = data["hits"]
    assert [h["chunk_index"] for h in hits] == [0, 1] and hits[0]["score"] > hits[1]["score"]
    assert hits[0]["text"] == TEXT[a : b - 1] and hits[0]["synthetic_status"] == "synthetic"
    assert (hits[0]["start_offset"], hits[0]["end_offset"]) == (a, b - 1) and hits[0]["resident_id"] == str(
        seeded["resident"]
    )
    assert (
        api.get("/api/v1/evidence/search", params={"q": "breast", "resident_id": str(uuid.uuid4())}).json()[
            "data"
        ]["hits"]
        == []
    )
    assert api.get("/api/v1/evidence/search", params={"q": "x"}).status_code == 422  # too short
    assert len(sink.records) == 2 and all(rec.task == "embedding.document" for rec in sink.records)

    api.state.ai_router = make_test_router(
        {"nebius": EmbedAdapter()}, daily_budget_usd=Decimal("0.0000000001")
    )[0]
    assert api.get("/api/v1/evidence/search", params={"q": "breast"}).status_code == 429
    api.state.ai_router = make_test_router(
        {"nebius": FakeAdapter(err("unavailable"), err("unavailable"), err("unavailable"))}
    )[0]
    assert api.get("/api/v1/evidence/search", params={"q": "breast"}).status_code == 503


def test_search_requires_a_role_that_may_trigger_ai(api, seeded):
    api.state.ai_router = make_test_router({"nebius": EmbedAdapter()})[0]
    for role, status in (
        ("clinical_reviewer", 200),
        ("population_admin", 200),
        ("system_admin", 403),
        ("observer", 403),
    ):
        api.as_role(role)
        assert api.get("/api/v1/evidence/search", params={"q": "breast"}).status_code == status, role


def test_unused_imports_guard():
    assert AdapterResponse and ok
