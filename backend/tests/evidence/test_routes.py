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

from anticipa.ai.settings import AiSettings
from anticipa.ai.testing import FakeAdapter, err, make_test_router, ok
from anticipa.ai.types import AdapterResponse, Usage
from anticipa.api.errors import install_exception_handlers
from anticipa.core.auth import CurrentUser, get_current_user
from anticipa.core.models import AuditEvent
from anticipa.datafabric.models import DocumentChunk
from anticipa.evidence import DocumentMeta, extract_document, service
from anticipa.evidence import routes as routes_mod
from anticipa.evidence.cohort import save_manifest, select_cohort
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
    """In-memory document store (documents and cohort manifests)."""

    def __init__(self, texts):
        self.texts = texts

    def read_text(self, key):
        if key not in self.texts:
            raise FileNotFoundError(key)  # the DocumentStore contract
        return self.texts[key]

    def write_text(self, key, text_):
        self.texts[key] = text_
        return key

    def exists(self, key):
        return key in self.texts


def settings(**kw) -> AiSettings:
    return AiSettings(_env_file=None, **kw)


@pytest.fixture
def seeded(world, sessions, monkeypatch):
    """One resident, one document, three extracted facts (accepted / rejected / requires_review), a cohort."""
    rid = world.resident(preferred_language="en")
    did = world.document(rid, TEXT)
    key = f"{world.tag}/{did}.txt"
    store = Store({key: TEXT})
    monkeypatch.setattr(service, "get_store", lambda: store)
    monkeypatch.setattr(routes_mod, "get_ai_settings", lambda: settings())  # default policy: Nebius off
    router, _, _ = make_test_router({"anthropic": FakeAdapter(ok(REPLY))})
    outcome = asyncio.run(extract_document(router, TEXT, DocumentMeta(document_id=str(did))))
    doc = service.DocRow(did, rid, "clinical_note", "en", key, "h", None, "synthetic")
    with sessions() as s:
        service.persist_outcome(s, doc, outcome, TEXT)
        s.commit()
        facts = {
            f.fact_type: f.id
            for f in s.execute(select(EvidenceFact).where(EvidenceFact.resident_id == rid)).scalars()
        }
        manifest = select_cohort(s, seed=1, size=10, dataset_version=f"test-{world.tag}")
    save_manifest(store, manifest)
    return {
        "resident": rid, "document": did, "facts": facts, "key": key, "store": store, "cohort": manifest.cohort_id,
    }  # fmt: skip


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
    assert fact["synthetic_status"] == "synthetic" and fact["model_provider"] == "anthropic"

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
        api.get("/api/v1/evidence/search", params={"q": "breast"}).status_code == 200
    )  # full text: no provider
    assert api.get("/api/v1/evidence/search", params={"q": "breast", "mode": "semantic"}).status_code == 409
    api.as_role("care_coordinator")  # no evidence.view at all
    assert api.get("/api/v1/evidence").status_code == 403


# ------------------------------------------------------------------------------------------------- jobs
def test_extraction_job_needs_a_cohort_and_is_validated_estimated_and_idempotent(api, seeded, world):
    api.as_role("population_admin")
    body = {
        "cohort_id": seeded["cohort"],
        "resident_ids": [str(seeded["resident"])],
        "idempotency_key": f"k-{world.tag}",
    }
    r = api.post("/api/v1/evidence/extraction-jobs", json=body)
    assert r.status_code == 202
    first = r.json()["data"]
    world.jobs.append(uuid.UUID(first["job_id"]))
    assert first["job_type"] == "evidence.extract_batch" and first["created"] and first["document_count"] == 1
    est = first["estimate"]
    assert est["documents"] == 1 and est["max_escalations"] == 0  # 10% of a 1-document cohort is 0
    assert Decimal(est["expected_total_usd"]) == Decimal("0.0004") and est["within_run_budget"] is True
    again = api.post("/api/v1/evidence/extraction-jobs", json=body)
    assert again.status_code == 200 and again.json()["data"]["job_id"] == first["job_id"]
    for bad in (
        {},
        {"limit": 5},
        {"limit": 5, "cohort_id": "no-such-cohort-x"},
        {"limit": 0},
        {"cohort_id": seeded["cohort"], "surprise": 1},
        {"cohort_id": seeded["cohort"], "concurrency": 99},
    ):
        assert api.post("/api/v1/evidence/extraction-jobs", json=bad).status_code in (404, 422), bad
    dry = api.post("/api/v1/evidence/extraction-jobs", json={"cohort_id": seeded["cohort"], "dry_run": True})
    assert dry.status_code == 200 and dry.json()["data"]["dry_run"] and dry.json()["data"]["job_id"] is None


def test_scope_beyond_the_cohort_is_refused_unless_an_audited_override_is_given(api, seeded, world, sessions):
    api.as_role("population_admin")
    other = world.resident(preferred_language="hi")  # created after the cohort manifest: not a member
    other_doc = world.document(other, "Another resident's note.")
    outside = {"cohort_id": seeded["cohort"], "document_ids": [str(other_doc)]}
    r = api.post("/api/v1/evidence/extraction-jobs", json=outside)
    assert r.status_code == 422 and "outside AI cohort" in r.json()["error"]["message"]
    assert (
        api.post(
            "/api/v1/evidence/extraction-jobs",
            json={"cohort_id": seeded["cohort"], "resident_ids": [str(other)]},
        ).status_code
        == 422
    )
    over = api.post(
        "/api/v1/evidence/extraction-jobs", json={"override_cohort": True, "document_ids": [str(other_doc)]}
    )
    assert over.status_code == 202 and over.json()["data"]["override_cohort"] is True
    world.jobs.append(uuid.UUID(over.json()["data"]["job_id"]))
    with sessions() as s:
        actions = {
            e.action: e.details
            for e in s.execute(
                select(AuditEvent).where(AuditEvent.resource_id == over.json()["data"]["job_id"])
            ).scalars()
        }
    assert actions["evidence.cohort.override"]["documents"] == 1
    assert actions["evidence.extraction.submit"]["override_cohort"] is True


def test_a_run_whose_expected_cost_exceeds_the_run_budget_is_refused(api, seeded, monkeypatch):
    api.as_role("population_admin")
    monkeypatch.setattr(routes_mod, "get_ai_settings", lambda: settings(anticipa_ai_run_budget_usd="0.0001"))
    r = api.post(
        "/api/v1/evidence/extraction-jobs",
        json={"cohort_id": seeded["cohort"], "resident_ids": [str(seeded["resident"])]},
    )
    assert r.status_code == 409 and "per-run budget" in r.json()["error"]["message"]


def test_embeddings_are_disabled_by_default_and_need_an_approved_cost_when_enabled(
    api, seeded, world, monkeypatch
):
    api.as_role("system_admin")
    body = {"cohort_id": seeded["cohort"]}
    off = api.post("/api/v1/evidence/embedding-jobs", json=body)
    assert off.status_code == 409 and "disabled" in off.json()["error"]["message"]  # default: Nebius off

    monkeypatch.setattr(routes_mod, "get_ai_settings", lambda: settings(anticipa_enable_nebius=True))
    dry = api.post("/api/v1/evidence/embedding-jobs", json={**body, "dry_run": True}).json()["data"]
    est = dry["estimate"]
    assert dry["dry_run"] and est["unique_texts_to_embed"] == 1 and est["estimated_tokens"] > 0
    assert est["provider"] == "nebius" and est["documents"] == 1 and Decimal(est["estimated_cost_usd"]) > 0
    refused = api.post("/api/v1/evidence/embedding-jobs", json=body)  # no approval flag: refuses to start
    assert refused.status_code == 409 and "approval required" in refused.json()["error"]["message"]
    low = api.post("/api/v1/evidence/embedding-jobs", json={**body, "approve_cost_usd": "0.000000"})
    assert low.status_code == 409
    approved = api.post("/api/v1/evidence/embedding-jobs", json={**body, "approve_cost_usd": "0.01"})
    assert approved.status_code == 202 and approved.json()["data"]["job_type"] == "evidence.embed_documents"
    world.jobs.append(uuid.UUID(approved.json()["data"]["job_id"]))
    monkeypatch.setattr(
        routes_mod,
        "get_ai_settings",
        lambda: settings(anticipa_enable_nebius=True, anticipa_nebius_run_approved_usd="0.01"),
    )
    env_ok = api.post("/api/v1/evidence/embedding-jobs", json=body)  # the env approval works too
    assert env_ok.status_code == 202
    world.jobs.append(uuid.UUID(env_ok.json()["data"]["job_id"]))


# --------------------------------------------------------------------------------------------- cohorts
def test_cohort_creation_is_deterministic_idempotent_audited_and_capped(api, seeded, world, sessions):
    api.as_role("population_admin")
    body = {"seed": 7, "size": 5, "dataset_version": f"test-{world.tag}", "cohort_id": f"c-{world.tag}"}
    r = api.post("/api/v1/evidence/cohorts", json=body)
    assert r.status_code == 201
    m = r.json()["data"]
    assert (
        m["size"] == 1 and m["cohort_id"] == body["cohort_id"] and "resident_ids" not in m
    )  # one eligible resident
    assert m["distributions"]["language"] == {"en": 1} and m["strata"] == 1
    again = api.post("/api/v1/evidence/cohorts", json=body)
    assert (
        again.status_code == 200 and again.json()["data"]["resident_ids_sha256"] == m["resident_ids_sha256"]
    )
    got = api.get(f"/api/v1/evidence/cohorts/{body['cohort_id']}")
    assert got.status_code == 200 and got.json()["data"]["seed"] == 7
    assert api.get("/api/v1/evidence/cohorts/missing-cohort").status_code == 404
    assert (
        api.post("/api/v1/evidence/cohorts", json={"seed": 1, "size": 10_001}).status_code == 422
    )  # fixed at 10k
    world.residents.append(world.resident(preferred_language="ar"))
    world.document(world.residents[-1], "A new resident arrives.")
    changed = api.post("/api/v1/evidence/cohorts", json={**body, "size": 5})
    assert changed.status_code == 409  # a registered cohort is never silently replaced
    with sessions() as s:
        assert s.execute(
            select(AuditEvent).where(
                AuditEvent.action == "evidence.cohort.create", AuditEvent.resource_id == body["cohort_id"]
            )
        ).scalar_one()
    api.as_role("observer")
    assert api.post("/api/v1/evidence/cohorts", json=body).status_code == 403
    assert api.get(f"/api/v1/evidence/cohorts/{body['cohort_id']}").status_code == 200


# ----------------------------------------------------------------------------------------------- search
HI = "माँ को 45 वर्ष की आयु में स्तन कैंसर का निदान हुआ था। मरीज़ को मधुमेह नहीं है।"
AR = "تم تشخيص الأم بسرطان الثدي في سن 45. لا يوجد داء السكري لدى المريضة."


def test_default_search_is_full_text_with_offsets_for_english_hindi_and_arabic(api, seeded, world):
    rid, store = seeded["resident"], seeded["store"]
    for lang, text_ in (("hi", HI), ("ar", AR)):
        did = world.document(rid, text_, language=lang)
        store.texts[f"{world.tag}/{did}.txt"] = text_
    api.as_role("observer")  # full text calls no provider, so every *.view role may use it
    r = api.get("/api/v1/evidence/search", params={"q": "breast cancer", "resident_id": str(rid)})
    assert r.status_code == 200
    data = r.json()["data"]
    assert (
        data["mode"] == "fulltext" and data["retrieval_only"] is True and "never establish" in data["notice"]
    )
    assert (
        data["match"] == "all_terms"
        and data["semantic_available"] is False
        and data["scanned_documents"] == 3
    )
    hit = data["hits"][0]
    assert (
        hit["chunk_id"] is None
        and hit["text"] == TEXT[hit["start_offset"] : hit["end_offset"]]
        and "breast" in hit["text"]
    )
    assert hit["resident_id"] == str(rid) and hit["synthetic_status"] == "synthetic" and hit["score"] > 0

    for query, needle, doc_lang in (("स्तन कैंसर", "स्तन", "hi"), ("سرطان الثدي", "الثدي", "ar")):
        hits = api.get("/api/v1/evidence/search", params={"q": query, "resident_id": str(rid)}).json()[
            "data"
        ]["hits"]
        assert hits and hits[0]["language"] == doc_lang and needle in hits[0]["text"], query
    # no chunk has every term: terms are OR-ed with the same PostgreSQL tokeniser
    anyt = api.get(
        "/api/v1/evidence/search", params={"q": "mammogram colonoscopy", "resident_id": str(rid)}
    ).json()["data"]
    assert anyt["match"] == "any_term" and "Colonoscopy" in anyt["hits"][0]["text"]
    nothing = api.get("/api/v1/evidence/search", params={"q": "zzzqqq", "resident_id": str(rid)}).json()[
        "data"
    ]
    assert nothing["hits"] == [] and nothing["match"] is None
    assert api.get("/api/v1/evidence/search", params={"q": "x"}).status_code == 422  # too short
    assert (
        api.get("/api/v1/evidence/search", params={"q": "breast", "resident_id": str(uuid.uuid4())}).json()[
            "data"
        ]["hits"]
        == []
    )


def test_unscoped_full_text_search_is_capped_and_says_so(api, seeded, monkeypatch):
    monkeypatch.setattr(routes_mod, "FTS_SCAN_DOCUMENTS", 0)
    data = api.get("/api/v1/evidence/search", params={"q": "breast"}).json()["data"]
    assert data["truncated"] is True and data["scanned_documents"] == 0


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


def vec(x0, x1):
    v = [0.0] * 1024
    v[0], v[1], v[1023] = x0, x1, 0.1
    return v


@pytest.fixture
def embedded(seeded, sessions, monkeypatch):
    a, b = TEXT.index("Mother"), TEXT.index("Colonoscopy")
    with sessions() as s:
        s.add_all([
            DocumentChunk(document_id=seeded["document"], chunk_index=0, start_offset=a, end_offset=b - 1, content_hash="a",
                          embedding=vec(1, 0), embedding_model="m"),
            DocumentChunk(document_id=seeded["document"], chunk_index=1, start_offset=b, end_offset=len(TEXT), content_hash="b",
                          embedding=vec(0, 1), embedding_model="m"),
        ])  # fmt: skip
        s.commit()
    return {**seeded, "a": a, "b": b}


def test_semantic_search_is_opt_in_ranked_scoped_and_budget_guarded(api, embedded, monkeypatch):
    rid, a, b = embedded["resident"], embedded["a"], embedded["b"]
    router, sink, _ = make_test_router({"nebius": EmbedAdapter()})
    api.state.ai_router = router
    # Nebius disabled (default): embeddings exist but are never used; the app works through full text
    off = api.get(
        "/api/v1/evidence/search", params={"q": "breast cancer family", "resident_id": str(rid)}
    ).json()["data"]
    assert off["mode"] == "fulltext" and off["semantic_available"] is False and sink.records == []
    assert api.get("/api/v1/evidence/search", params={"q": "breast", "mode": "semantic"}).status_code == 409

    monkeypatch.setattr(routes_mod, "get_ai_settings", lambda: settings(anticipa_enable_nebius=True))
    r = api.get("/api/v1/evidence/search", params={"q": "breast cancer family", "resident_id": str(rid)})
    data = r.json()["data"]
    assert (
        data["mode"] == "semantic" and data["semantic_available"] and data["query_model"].startswith("Qwen")
    )
    hits = data["hits"]
    assert [h["chunk_index"] for h in hits] == [0, 1] and hits[0]["score"] > hits[1]["score"]
    assert (
        hits[0]["text"] == TEXT[a : b - 1]
        and hits[0]["chunk_id"]
        and hits[0]["synthetic_status"] == "synthetic"
    )
    assert len(sink.records) == 1 and sink.records[0].task == "embedding.document"
    forced_ft = api.get(
        "/api/v1/evidence/search", params={"q": "breast", "mode": "fulltext", "resident_id": str(rid)}
    ).json()["data"]
    assert (
        forced_ft["mode"] == "fulltext" and forced_ft["semantic_available"] is True and len(sink.records) == 1
    )

    api.state.ai_router = make_test_router(
        {"nebius": EmbedAdapter()}, daily_budget_usd=Decimal("0.0000000001")
    )[0]
    assert api.get("/api/v1/evidence/search", params={"q": "breast"}).status_code == 429
    api.state.ai_router = make_test_router(
        {"nebius": FakeAdapter(err("unavailable"), err("unavailable"), err("unavailable"))}
    )[0]
    assert api.get("/api/v1/evidence/search", params={"q": "breast"}).status_code == 503


def test_semantic_search_requires_a_role_that_may_trigger_ai(api, embedded, monkeypatch):
    monkeypatch.setattr(routes_mod, "get_ai_settings", lambda: settings(anticipa_enable_nebius=True))
    api.state.ai_router = make_test_router({"nebius": EmbedAdapter()})[0]
    for role, mode, status, used in (
        ("clinical_reviewer", "semantic", 200, "semantic"),
        ("population_admin", "auto", 200, "semantic"),
        ("observer", "auto", 200, "fulltext"),  # auto quietly stays on full text for a read-only role
        ("observer", "semantic", 403, None),
        ("system_admin", "auto", 200, "semantic"),  # holds every product view since the 2026-11-13 role change, and may trigger AI
    ):
        api.as_role(role)
        r = api.get("/api/v1/evidence/search", params={"q": "breast", "mode": mode})
        assert r.status_code == status, (role, mode)
        if used:
            assert r.json()["data"]["mode"] == used, (role, mode)


def test_unused_imports_guard():
    assert AdapterResponse and ok and update and err
