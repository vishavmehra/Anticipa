"""Resident journey, pedigree and engagement preview (integration, DB `anticipa_test`, rolled back): one payload with
facts (source ids, quotes, offsets, verification), evaluations (rule / guideline versions, review status), cases with
history, allocations, appointments, communications in every language and completion evidence; RBAC section gating;
pedigree built only from stored family edges; the simulated template preview."""
# ruff: noqa: E501

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, select

from anticipa.analytics import service
from anticipa.caregaps import service as caregaps
from anticipa.caregaps.models import CareGapCase
from anticipa.closure.models import CompletionEvidence
from anticipa.core.audit import Actor
from anticipa.datafabric.models import ClinicalRecord, Dataset, FamilyEdge
from anticipa.engagement.models import (
    Appointment,
    CommunicationEvent,
    CommunicationTemplate,
    ResidentResponse,
)
from anticipa.engagement.templates import SIMULATION_MARKERS, ensure_templates
from anticipa.evidence.models import EvidenceSource
from anticipa.jobs.models import ProcessingJob

API = "/api/v1"
QUOTE = "Her mother was treated for breast cancer."


def get(client, headers, path, status=200):
    r = client.get(f"{API}{path}", headers=headers)
    assert r.status_code == status, r.text
    return r.json()["data"] if status == 200 else r.json()["error"]


# ------------------------------------------------------------------------------------------------ journey
def build_journey(seed, seed_module, world, db_session):
    version, _ = seed.approved_version()
    resident = seed.resident()
    doc = seed.document(resident)
    fact = seed.fact(resident, fact_type="breast_cancer", relationship="mother", quote=QUOTE, document_id=doc.id)
    src = db_session.scalar(select(EvidenceSource).where(EvidenceSource.evidence_fact_id == fact.id))
    src.source_start_offset, src.source_end_offset = 10, 10 + len(QUOTE)
    db_session.flush()
    seed_module.run_evaluation(db_session, [version], date(2026, 11, 8), resident_ids=[resident.id])
    caregaps.create_cases_scan(seed_module.session_factory(db_session), [version.id])
    case = db_session.execute(select(CareGapCase).where(CareGapCase.resident_id == resident.id)).scalar_one()
    caregaps.transition(db_session, case, "planned", actor=Actor.system())
    alloc = world.allocation(case, confirm=False)
    db_session.add(Appointment(
        care_gap_case_id=case.id, resident_id=resident.id, clinic_id=alloc.clinic_id, clinic_service_id=alloc.clinic_service_id,
        capacity_slot_id=alloc.capacity_slot_id, planned_allocation_id=alloc.id, start_time=alloc.proposed_start_time,
        end_time=alloc.proposed_end_time, appointment_status="proposed"))  # fmt: skip
    events = [
        CommunicationEvent(care_gap_case_id=case.id, resident_id=resident.id, channel="sms", template_key="preventive_care_reminder",
                           template_version=1, language=lang, body=f"[SIMULATED] {lang}", delivery_state="delivered")
        for lang in ("en", "hi")
    ]  # fmt: skip
    db_session.add_all(events)
    db_session.flush()
    db_session.add(ResidentResponse(communication_event_id=events[0].id, care_gap_case_id=case.id, resident_id=resident.id, response_type="accept"))
    db_session.add(CompletionEvidence(
        care_gap_case_id=case.id, activity=case.care_activity, source_type="simulated_procedure_record", evidence_reference="sim://proc/1",
        verification_status="verified", verification_method="simulated_record", verified_at=datetime.now(UTC)))  # fmt: skip
    db_session.flush()
    return resident, doc, fact, case, alloc


def test_journey_returns_everything_in_one_payload(api_client, auth_headers, seed, seed_module, world, db_session):
    resident, doc, fact, case, alloc = build_journey(seed, seed_module, world, db_session)
    data = get(api_client, auth_headers("observer"), f"/residents/{resident.id}/journey")
    assert data["restricted"] == [] and data["synthetic_banner"] and data["sources"]["communications"] == "simulated"
    assert data["resident"]["id"] == str(resident.id) and data["resident"]["synthetic_status"] == "synthetic"
    assert data["resident"]["counts"] == {"clinical_records": 1, "family_edges": 0, "documents": 1, "evidence_facts": 1}

    ev = data["evidence"]
    f = ev["facts"][0]
    assert f["id"] == str(fact.id) and f["fact_type"] == "breast_cancer" and f["relationship_type"] == "mother"
    s = f["sources"][0]
    assert (s["document_id"], s["quote"], s["start_offset"], s["end_offset"], s["verification_status"]) == (
        str(doc.id), QUOTE, 10, 10 + len(QUOTE), "verified_exact")
    assert ev["documents"][0]["id"] == str(doc.id) and ev["counts_by_validation_status"] == {"accepted": 1}

    e = data["evaluations"]["items"][0]
    assert e["is_latest"] and e["outcome"] in ("eligible", "requires_clinical_review") and e["review_status"]
    assert e["guideline"]["version"] == 1 and e["guideline"]["key"].startswith("test-") and e["guideline"]["clinical_review_status"]
    assert e["explanation"] and e["input_hash"] and e["evaluation_date"] == "2026-11-08"

    c = data["cases"]["items"][0]
    assert c["id"] == str(case.id) and c["status"] == "planned" and c["evaluation_id"] == e["id"]
    assert [h["to_status"] for h in c["history"]][-1] == "planned" and "actor_id" not in c["history"][0]
    assert c["allocations"][0]["id"] == str(alloc.id) and c["allocations"][0]["clinic"]["name"] and c["allocations"][0]["simulated"] is True
    assert c["appointments"][0]["appointment_status"] == "proposed" and c["appointments"][0]["clinic"]["key"] == world.clinic.clinic_key
    assert sorted(m["language"] for m in c["communications"]) == ["en", "hi"]
    en = next(m for m in c["communications"] if m["language"] == "en")  # same-transaction rows share created_at: pick by language
    assert en["direction"] == "ltr" and en["responses"][0]["response_type"] == "accept"
    assert c["completion_evidence"][0]["verification_status"] == "verified" and c["completion_evidence"][0]["simulated"] is True


def test_journey_gates_sections_by_permission(api_client, auth_headers, seed, seed_module, world, db_session):
    resident, *_ = build_journey(seed, seed_module, world, db_session)
    # care_coordinator holds residents/rules/caregaps/capacity/appointments/engagement/closure views but not evidence.view
    data = get(api_client, auth_headers("care_coordinator"), f"/residents/{resident.id}/journey")
    assert data["evidence"] is None and data["restricted"] == ["evidence"]
    assert data["evaluations"]["items"] and data["cases"]["items"][0]["communications"]
    # clinical_reviewer has no capacity / appointment / engagement views
    data = get(api_client, auth_headers("clinical_reviewer"), f"/residents/{resident.id}/journey")
    case = data["cases"]["items"][0]
    assert case["allocations"] is None and case["appointments"] is None and case["communications"] is None
    assert set(data["restricted"]) == {"allocations", "appointments", "communications"} and data["evidence"]["facts"]


def test_journey_unknown_resident_and_bad_id(api_client, auth_headers):
    h = auth_headers("observer")
    assert get(api_client, h, f"/residents/{uuid.uuid4()}/journey", 404)["code"] == "NOT_FOUND"
    assert get(api_client, h, "/residents/not-a-uuid/journey", 422)["code"] == "VALIDATION_ERROR"


def test_journey_of_a_resident_without_data_has_empty_sections(api_client, auth_headers, seed):
    r = seed.resident(covered=False)
    data = get(api_client, auth_headers("observer"), f"/residents/{r.id}/journey")
    assert data["evidence"]["facts"] == [] and data["evaluations"]["items"] == [] and data["cases"]["items"] == []
    assert data["resident"]["counts"]["clinical_records"] == 0


# ------------------------------------------------------------------------------------------------ pedigree
def edge(resident, related, rtype, **kw):
    return FamilyEdge(resident_id=resident.id, related_resident_id=getattr(related, "id", None), relationship_type=rtype,
                      relationship_direction="related_to_resident", source_type="deterministic_linker",
                      confidence_status="verified", **kw)  # fmt: skip


def build_pedigree(seed, db_session):
    proband = seed.resident()
    mother, sister1, sister2 = seed.resident(), seed.resident(), seed.resident()
    doc = seed.document(proband)
    db_session.add_all([
        edge(proband, mother, "mother"), edge(proband, sister1, "sister"), edge(proband, sister2, "sister"),
        edge(proband, None, "father", related_person_source_id="doc-only-father"),
    ])  # fmt: skip
    for display in ("Malignant neoplasm of breast (disorder)", "Essential hypertension (disorder)"):
        db_session.add(ClinicalRecord(resident_id=mother.id, resource_type="Condition", source_resource_id=uuid.uuid4().hex, category="condition",
                                      code_system="urn:test:synthetic", code="C-1", display=display, status="active",
                                      effective_start=datetime(2015, 1, 1, tzinfo=UTC), source_reference=f"synthea://test/{display[:5]}"))  # fmt: skip
    db_session.flush()
    stated = seed.fact(proband, fact_type="breast_cancer", relationship="mother", quote="Mother had breast cancer.", document_id=doc.id)
    seed.fact(proband, fact_type="diabetes", relationship="mother")  # not cancer: excluded
    seed.fact(proband, fact_type="prostate_cancer", relationship="father", validation="requires_review")  # not accepted: excluded
    seed.fact(proband, fact_type="breast_cancer", relationship="sister", quote="A sister had breast cancer.", document_id=doc.id)  # two sisters
    seed.fact(proband, fact_type="ovarian_cancer", relationship="maternal_aunt")  # no stored aunt
    own = seed.fact(mother, category="condition", fact_type="breast_cancer", subject_type="resident", relationship=None)
    return proband, mother, stated, own


def test_pedigree_is_built_only_from_stored_edges(api_client, auth_headers, seed, db_session):
    proband, mother, stated, own = build_pedigree(seed, db_session)
    data = get(api_client, auth_headers("observer"), f"/residents/{proband.id}/pedigree")
    rel = data["relatives"]
    assert [r["relationship"] for r in rel] == ["father", "mother", "sister", "sister"]  # exactly the 4 stored edges
    assert data["counts"] == {"relatives": 4, "linked_relatives": 3, "relatives_with_cancer_history": 1}
    by = {(r["relationship"], r["linked_resident"] is not None): r for r in rel}
    m = by[("mother", True)]
    assert m["degree"] == 1 and m["degree_label"] == "first degree" and m["blood_relative"] is True and m["direction"] == "related_to_resident"
    assert m["linked_resident"]["id"] == str(mother.id) and m["confidence_status"] == "verified"
    recs = m["cancer_history"]["clinical_records"]
    assert [x["display"] for x in recs] == ["Malignant neoplasm of breast (disorder)"] and recs[0]["source_reference"].startswith("synthea://")
    facts = {f["id"]: f for f in m["cancer_history"]["evidence_facts"]}
    assert set(facts) == {str(stated.id), str(own.id)}  # the diabetes fact is not cancer-related
    assert facts[str(stated.id)]["basis"] == "stated_in_resident_document" and facts[str(stated.id)]["sources"][0]["quote"] == "Mother had breast cancer."
    assert facts[str(own.id)]["basis"] == "relative_own_document"
    father = by[("father", False)]
    assert father["related_person_source_id"] == "doc-only-father" and father["cancer_history"] == {"clinical_records": [], "evidence_facts": []}
    # the sister fact matches two stored sisters, the aunt fact matches none: listed, never added as a relative
    reasons = {(f["relationship_type"], f["reason"]) for f in data["unattributed_family_facts"]}
    assert reasons == {("sister", "multiple_family_edges_match_the_relationship"), ("maternal_aunt", "no_family_edge_for_the_relationship")}
    assert all(r["cancer_history"]["evidence_facts"] == [] for r in rel if r["relationship"] == "sister")
    assert data["definition"] and data["restricted"] == []


def test_pedigree_gates_evidence_by_permission(api_client, auth_headers, seed, db_session):
    proband, *_ = build_pedigree(seed, db_session)
    data = get(api_client, auth_headers("care_coordinator"), f"/residents/{proband.id}/pedigree")  # records.view yes, evidence.view no
    assert data["restricted"] == ["evidence_facts"] and data["unattributed_family_facts"] is None
    mother = next(r for r in data["relatives"] if r["relationship"] == "mother")
    assert mother["cancer_history"]["evidence_facts"] is None and len(mother["cancer_history"]["clinical_records"]) == 1


def test_pedigree_of_a_resident_without_family_is_empty(api_client, auth_headers, seed):
    r = seed.resident()
    data = get(api_client, auth_headers("observer"), f"/residents/{r.id}/pedigree")
    assert data["relatives"] == [] and data["unattributed_family_facts"] == [] and data["counts"]["relatives"] == 0
    assert get(api_client, auth_headers("observer"), f"/residents/{uuid.uuid4()}/pedigree", 404)["code"] == "NOT_FOUND"


# ------------------------------------------------------------------------------------------------ engagement preview
def test_preview_renders_every_language_without_writing(api_client, auth_headers, world, db_session):
    case = world.case("planned", "hi", activity="colorectal_cancer_screening")
    before = db_session.scalar(select(func.count()).select_from(CommunicationTemplate))
    data = get(api_client, auth_headers("observer"), f"/engagement/preview?case_id={case.id}")
    assert [p["language"] for p in data["previews"]] == ["en", "hi", "ar"]
    assert data["simulated"] is True and data["ai_calls"] == 0 and data["synthetic_status"] == "simulated" and data["template_version"] == 1
    assert data["resident_preferred_language"] == "hi" and data["default_language"] == "hi"
    for p in data["previews"]:
        assert SIMULATION_MARKERS[p["language"]] in p["body"] and p["constraints_checked"] and p["characters"] == len(p["body"])
        assert p["is_resident_preferred_language"] == (p["language"] == "hi") and p["template_stored"] is False
    assert [p["direction"] for p in data["previews"]] == ["ltr", "ltr", "rtl"]
    assert "colorectal cancer screening" in data["previews"][0]["body"] and "कोलोरेक्टल" in data["previews"][1]["body"]
    assert db_session.scalar(select(func.count()).select_from(CommunicationTemplate)) == before  # nothing stored


def test_preview_reports_stored_templates_and_honours_the_language_list(api_client, auth_headers, world, db_session):
    case = world.case("planned", "en")
    ensure_templates(db_session)
    data = get(api_client, auth_headers("observer"), f"/engagement/preview?case_id={case.id}&languages=ar,en,ar")
    assert [p["language"] for p in data["previews"]] == ["ar", "en"]  # order kept, duplicates dropped
    assert all(p["template_stored"] and p["template_approved"] for p in data["previews"])


def test_preview_validation_and_not_found(api_client, auth_headers, world):
    case = world.case("planned", "en")
    h = auth_headers("observer")
    assert get(api_client, h, f"/engagement/preview?case_id={case.id}&languages=en,fr", 422)["code"] == "VALIDATION_ERROR"
    assert get(api_client, h, f"/engagement/preview?case_id={uuid.uuid4()}", 404)["code"] == "NOT_FOUND"
    assert get(api_client, h, "/engagement/preview", 422)["code"] == "VALIDATION_ERROR"  # case_id is required
    assert len(get(api_client, h, f"/engagement/preview?case_id={case.id}&languages=")["previews"]) == 3  # blank = all


# ------------------------------------------------------------------------------------------------ UI text rules
BANNED_TEXT = re.compile(r"[-–—]|synthetic|illustrative|pending review", re.I)
TEXT_KEYS = ("definition", "label", "message", "note", "notice", "reading", "synthetic_banner", "degree_label")
DATA_KEYS = {"explanation", "source_references", "evidence_references", "missing_information", "conflicting_information",
             "exclusion_reasons", "details", "value"}  # stored data (citations, rule explanations), not text this module writes


def human_text(node, path=""):
    """Yield (path, text) for the human readable strings: keys named like TEXT_KEYS, *_basis and *_note, and the legend of `sources`."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in DATA_KEYS:
                continue
            if isinstance(v, str) and (k in TEXT_KEYS or k.endswith(("_basis", "_note")) or (k == "title" and path.startswith(".alert"))):
                yield f"{path}.{k}", v
            elif k == "sources" and isinstance(v, dict) and not path:
                yield from ((f"sources.{sk}", sv) for sk, sv in v.items() if sk in service.SOURCES)
            else:
                yield from human_text(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from human_text(v, f"{path}[{i}]")


def test_ui_text_has_no_hyphens_dashes_or_banned_wording(api_client, auth_headers, seed, seed_module, world, db_session, tmp_path):
    resident, *_ = build_journey(seed, seed_module, world, db_session)
    proband, *_ = build_pedigree(seed, db_session)
    version = "syn-20261108-s77"  # a manifest and a loaded dataset so the pipeline text is rendered too
    d = tmp_path / "synthea" / version
    d.mkdir(parents=True)
    now = datetime.now(UTC)
    batch = {"index": 0, "status": "completed", "residents": 10, "finished_at": now.isoformat()}
    (d / "manifest.json").write_text(json.dumps({"resident_target": 100, "batch_size": 10, "status": "incomplete", "updated_at": now.isoformat(),
                                                 "batches": {"0": batch, "1": {**batch, "index": 1}}}), encoding="utf-8")  # fmt: skip
    db_session.add(Dataset(dataset_version=version, generator="test", manifest={"ingest": {"batches": {"0": {"residents_staged": 10, "loaded_at": now.isoformat()}}}}))
    db_session.add(ProcessingJob(job_type="test.failed", status="failed"))
    db_session.flush()
    case_id = db_session.scalar(select(CareGapCase.id).where(CareGapCase.resident_id == resident.id))
    h = auth_headers("observer")
    ops = auth_headers("system_admin")  # processing is system_admin only; its Command Center also shows jobs
    endpoints = {
        "command-center": "/analytics/command-center", "funnel": "/analytics/funnel", "processing": f"/analytics/processing?dataset_version={version}",
        "geography": "/analytics/geography?level=state&code=32", "programmes": "/analytics/programmes", "geography-state": "/analytics/geography?level=state&code=23",
        "compare": "/analytics/command-center?compare=previous&days=7", "alerts": "/analytics/alerts/summary", "scope": "/meta/scope",
        "explorer": "/analytics/population-explorer", "profile": f"/residents/{resident.id}/profile",
        "journey": f"/residents/{resident.id}/journey", "pedigree": f"/residents/{proband.id}/pedigree", "preview": f"/engagement/preview?case_id={case_id}",
    }  # fmt: skip
    seen = 0
    for name, path in endpoints.items():
        payload = get(api_client, ops if name in ("command-center", "processing") else h, path)
        texts = list(human_text(payload))
        assert len(texts) >= (1 if name in ("preview", "alerts", "scope") else 3), (name, "the walk found almost no text")
        seen += len(texts)
        for where, text in texts:
            assert not BANNED_TEXT.search(text), (name, where, text)
    assert seen > 60
    assert all(not BANNED_TEXT.search(t) for t in service.SOURCES.values())
    assert set(service.SOURCES) == {"live", "synthetic", "simulated", "evaluation"}  # the machine enum is unchanged
