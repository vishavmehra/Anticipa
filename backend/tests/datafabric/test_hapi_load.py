# ruff: noqa: E501
"""HAPI demo load: FHIR mapping (`fhir_map`), raw-batch extraction and the transaction-bundle client (`hapi_load`).

No database and no server: the HAPI client is driven through `httpx.MockTransport`.
"""

from __future__ import annotations

import gzip
import json
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
import orjson
import pytest

from anticipa.datafabric import fhir_map as fm
from anticipa.datafabric import hapi_load as hl

DS = "syn-test"
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
PID = "11111111-1111-1111-1111-111111111111"


# ------------------------------------------------------------------------------------------------ Synthea rewrite


def _encounter() -> dict:
    return {
        "resourceType": "Encounter",
        "id": "e1",
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-encounter"]},
        "subject": {"reference": f"Patient/{PID}"},
        "participant": [
            {
                "individual": {
                    "reference": "Practitioner?identifier=http://hl7.org/fhir/sid/us-npi|999",
                    "display": "Dr X",
                }
            }
        ],
        "serviceProvider": {
            "reference": "Organization?identifier=https://example.org|org1",
            "display": "Clinic",
        },
        "partOf": {"reference": "Encounter/not-loaded"},
        "reasonReference": [{"reference": "Condition/gone"}, {"reference": "Condition/c1"}],
    }


def test_prepare_synthea_rewrites_references_and_tags() -> None:
    stats: Counter[str] = Counter()
    out = fm.prepare_synthea(_encounter(), {("Patient", PID), ("Condition", "c1")}, stats, DS)
    assert out["participant"][0]["individual"] == {
        "type": "Practitioner",
        "identifier": {"system": "http://hl7.org/fhir/sid/us-npi", "value": "999"},
        "display": "Dr X",
    }
    assert out["serviceProvider"]["identifier"]["value"] == "org1"
    assert "partOf" not in out  # reference to a resource that is not loaded, nothing else to say
    assert out["reasonReference"] == [{"reference": "Condition/c1"}]  # the dangling list item is gone
    assert out["subject"] == {"reference": f"Patient/{PID}"}
    assert "profile" not in out["meta"]  # US Core is not installed on the server; the claim becomes a tag
    codes = {(t["system"], t["code"]) for t in out["meta"]["tag"]}
    assert (
        "urn:anticipa:synthea-profile",
        "http://hl7.org/fhir/us/core/StructureDefinition/us-core-encounter",
    ) in codes
    assert (fm.SYNTHETIC_STATUS, "synthetic") in codes and (fm.DATASET, DS) in codes
    assert stats == {"conditional_to_identifier": 2, "dangling_dropped": 2}


def test_dangling_reference_keeps_display() -> None:
    stats: Counter[str] = Counter()
    out = fm.prepare_synthea(
        {
            "resourceType": "Observation",
            "id": "o",
            "subject": {"reference": f"Patient/{PID}"},
            "performer": [{"reference": "Practitioner/zz", "display": "Dr Z"}],
        },
        {("Patient", PID)},
        stats,
        DS,
    )
    assert out["performer"] == [{"display": "Dr Z"}]


def test_prepare_patient_adds_resident_identity() -> None:
    resident = {"id": uuid.UUID(int=5), "district_code": "D07", "preferred_language": "hi"}
    out = fm.prepare_patient(
        {"resourceType": "Patient", "id": PID, "identifier": [{"system": "s", "value": "v"}]},
        resident,
        set(),
        Counter(),
        DS,
    )
    assert {"system": fm.RESIDENT_ID, "value": str(uuid.UUID(int=5))} in out["identifier"]
    assert out["extension"] == [{"url": "urn:anticipa:district", "valueCode": "D07"}]
    assert out["communication"][0]["language"]["coding"][0]["code"] == "hi"


def test_dependency_order_puts_referenced_resources_first() -> None:
    a = {"resourceType": "DiagnosticReport", "id": "dr", "result": [{"reference": "Observation/o2"}]}
    b = {"resourceType": "Observation", "id": "o1", "hasMember": [{"reference": "Observation/o2"}]}
    c = {"resourceType": "Observation", "id": "o2"}
    d = {"resourceType": "Patient", "id": "p"}
    order = [(r["resourceType"], r["id"]) for r in fm.dependency_order([a, b, c, d])]
    assert order.index(("Observation", "o2")) < order.index(("Observation", "o1"))
    assert order.index(("Observation", "o2")) < order.index(("DiagnosticReport", "dr"))
    assert len(order) == 4


def test_dependency_order_survives_a_cycle() -> None:
    a = {"resourceType": "Observation", "id": "a", "hasMember": [{"reference": "Observation/b"}]}
    b = {"resourceType": "Observation", "id": "b", "hasMember": [{"reference": "Observation/a"}]}
    assert len(fm.dependency_order([a, b])) == 2


# ------------------------------------------------------------------------------------------------ raw files


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def test_read_batch_returns_only_wanted_ids(tmp_path: Path) -> None:
    rows = [{"resourceType": "Condition", "id": f"c{i}", "text": {"div": '"id":"decoy"'}} for i in range(5)]
    _write_ndjson(tmp_path / "batch-0" / "Condition.ndjson.gz", rows)
    out = hl.read_batch(tmp_path, "batch-0", {"Condition": {"c1", "c3"}})
    assert [r["id"] for r in out["Condition"]] == ["c1", "c3"]


def test_read_batch_missing_resource_is_an_error(tmp_path: Path) -> None:
    _write_ndjson(tmp_path / "batch-0" / "Condition.ndjson.gz", [{"resourceType": "Condition", "id": "c1"}])
    with pytest.raises(hl.HapiLoadError, match="found 1 of 2"):
        hl.read_batch(tmp_path, "batch-0", {"Condition": {"c1", "c9"}})


# ------------------------------------------------------------------------------------------------ client


def _client(handler) -> hl.Hapi:
    hapi = hl.Hapi("http://localhost:8090/fhir")
    hapi.http = httpx.Client(transport=httpx.MockTransport(handler))
    return hapi


def _bundle_response(statuses: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "resourceType": "Bundle",
            "type": "transaction-response",
            "entry": [{"response": {"status": s}} for s in statuses],
        },
    )


def test_put_all_sends_one_transaction_with_stable_put_urls() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = orjson.loads(request.content)
        seen.append(body)
        return _bundle_response(["201 Created", "200 OK"])

    hapi = _client(handler)
    assert hapi.base == "http://127.0.0.1:8090/fhir"
    counts, failures = hapi.put_all(
        [{"resourceType": "Task", "id": "t1"}, {"resourceType": "Task", "id": "t2"}]
    )
    assert failures == []
    assert counts == {"Task:created": 1, "Task:updated": 1}
    assert [e["request"] for e in seen[0]["entry"]] == [
        {"method": "PUT", "url": "Task/t1"},
        {"method": "PUT", "url": "Task/t2"},
    ]


def test_put_all_falls_back_to_single_puts_and_reports_the_bad_resource() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                400, json={"resourceType": "OperationOutcome", "issue": [{"diagnostics": "bad bundle"}]}
            )
        if request.url.path.endswith("/bad"):
            return httpx.Response(
                422,
                json={"resourceType": "OperationOutcome", "issue": [{"diagnostics": "Invalid reference"}]},
            )
        return httpx.Response(201 if request.url.path.endswith("/new") else 200, json={})

    counts, failures = _client(handler).put_all(
        [
            {"resourceType": "Flag", "id": "new"},
            {"resourceType": "Flag", "id": "old"},
            {"resourceType": "Flag", "id": "bad"},
        ]
    )
    assert counts == {"Flag:created": 1, "Flag:updated": 1, "Flag:failed": 1}
    assert failures == [{"resource": "Flag/bad", "error": "HTTP 422: Invalid reference"}]


def test_ready_requires_r4() -> None:
    ok = _client(lambda r: httpx.Response(200, json={"fhirVersion": "4.0.1"}))
    assert ok.ready() == "4.0.1"
    stu3 = _client(lambda r: httpx.Response(200, json={"fhirVersion": "3.0.2"}))
    with pytest.raises(hl.HapiLoadError, match="need R4"):
        stu3.ready()


# ------------------------------------------------------------------------------------------------ write-back mapping


def _case(**over) -> dict:
    case = {
        "id": uuid.UUID(int=1), "evaluation_id": uuid.UUID(int=2), "guideline_version_id": uuid.UUID(int=3),
        "rule_version_id": uuid.UUID(int=4), "care_activity": "breast_cancer_screening", "status": "approved_for_planning",
        "priority": "high", "source_outcome": "eligible", "opened_at": NOW, "updated_at": NOW, "closed_at": None,
        "closure_reason": None, "synthetic_status": "synthetic",
    }  # fmt: skip
    return {**case, **over}


def test_case_maps_to_task_issue_flag_and_provenance() -> None:
    case = _case()
    task = fm.task(case, PID, DS)
    assert (task["status"], task["priority"], task["intent"]) == ("ready", "asap", "plan")
    assert task["for"] == {"reference": f"Patient/{PID}"} and task["focus"] == {
        "reference": f"DetectedIssue/{case['id']}"
    }
    assert task["businessStatus"]["coding"][0]["code"] == "approved_for_planning"
    assert {i["type"]["text"] for i in task["input"]} == {
        "guideline_version_id",
        "evaluation_id",
        "rule_version_id",
    }
    issue = fm.detected_issue(case, PID, DS)
    assert (issue["status"], issue["severity"], issue["patient"]["reference"]) == (
        "final",
        "high",
        f"Patient/{PID}",
    )
    assert "not a diagnosis" in issue["detail"]
    assert fm.detected_issue(_case(status="pending_review"), PID, DS)["status"] == "preliminary"
    assert fm.needs_flag(case) and not fm.needs_flag(_case(priority="routine"))
    assert fm.flag(case, PID, DS)["status"] == "active"
    prov = fm.provenance(case, PID, DS)
    assert [t["reference"].split("/")[0] for t in prov["target"]] == ["DetectedIssue", "Task"]
    assert prov["entity"][0]["what"]["identifier"]["value"] == str(case["evaluation_id"])


def test_closed_case_task_and_flag_carry_the_end() -> None:
    closed = _case(status="closed_not_completed", closed_at=NOW, closure_reason="resident_declined")
    task = fm.task(closed, PID, DS)
    assert task["status"] == "failed" and task["executionPeriod"]["end"] == NOW.isoformat()
    assert task["statusReason"] == {"text": "resident_declined"}
    flag = fm.flag(closed, PID, DS)
    assert flag["status"] == "inactive" and flag["period"]["end"] == NOW.isoformat()
    assert all(None not in r.values() for r in (task, flag))  # no JSON nulls


def test_every_case_status_has_a_task_status() -> None:
    from anticipa.caregaps.models import CASE_STATUSES

    assert set(CASE_STATUSES) == set(fm.TASK_STATUS)


def test_engagement_status_maps_cover_the_schema() -> None:
    from anticipa.engagement.models import APPOINTMENT_STATUSES, DELIVERY_STATES
    from anticipa.planning.models import ALLOCATION_STATUSES

    assert set(DELIVERY_STATES) == set(fm.COMM_STATUS)
    assert set(APPOINTMENT_STATUSES) == set(fm.APPOINTMENT_STATUS)
    assert set(ALLOCATION_STATUSES) == set(fm.ALLOCATION_STATUS)


def test_communication_event_response_and_message() -> None:
    event = {
        "id": uuid.UUID(int=10), "care_gap_case_id": uuid.UUID(int=1), "channel": "sms", "template_key": "invite", "template_version": 2,
        "language": "hi", "body": "Hello (simulated)", "delivery_state": "delivered", "sent_at": NOW, "delivered_at": NOW, "synthetic_status": "simulated",
    }  # fmt: skip
    comm = fm.communication_event(event, PID, DS)
    assert comm["status"] == "completed" and comm["language"] == "hi" and comm["received"] == NOW.isoformat()
    assert comm["basedOn"] == [{"reference": f"Task/{event['care_gap_case_id']}"}]
    assert (fm.SYNTHETIC_STATUS, "simulated") in {(t["system"], t["code"]) for t in comm["meta"]["tag"]}
    assert (
        fm.communication_event(
            {**event, "delivery_state": "failed", "sent_at": None, "delivered_at": None}, PID, DS
        )["status"]
        == "not-done"
    )
    response = fm.communication_response(
        {
            "id": uuid.UUID(int=11),
            "care_gap_case_id": uuid.UUID(int=1),
            "communication_event_id": event["id"],
            "response_type": "accept",
            "received_at": NOW,
        },
        PID,
        DS,
    )
    assert response["inResponseTo"] == [{"reference": f"Communication/{event['id']}"}]
    assert response["payload"][0]["contentString"] == "[accept]" and response["sender"] == {
        "reference": f"Patient/{PID}"
    }
    message = {
        "id": uuid.UUID(int=12),
        "care_gap_case_id": uuid.UUID(int=1),
        "role": "assistant",
        "body": "Reply",
        "language": "en",
        "source": "template",
        "created_at": NOW,
        "intent": None,
    }
    reply = fm.communication_message(message, PID, DS)
    assert reply["recipient"] == [{"reference": f"Patient/{PID}"}] and "Patient" not in reply["sender"].get(
        "reference", ""
    )
    asked = fm.communication_message(
        {
            **message,
            "role": "resident",
            "intent": "medical_question",
            "escalation_reason": "medical_question",
        },
        PID,
        DS,
    )
    assert asked["sender"] == {"reference": f"Patient/{PID}"}
    assert {"url": "urn:anticipa:intent", "valueCode": "medical_question"} in asked["extension"]


def test_appointment_and_allocation() -> None:
    service = {"service_key": "mammography", "service_name": "Mammography"}
    base = {"care_gap_case_id": uuid.UUID(int=1), "clinic_id": uuid.UUID(int=20), "status_reason": None}
    appt = fm.appointment(
        {
            **base,
            "id": uuid.UUID(int=30),
            "appointment_status": "simulated_confirmed",
            "start_time": NOW,
            "end_time": NOW,
            "synthetic_status": "simulated",
        },
        PID,
        service,
        DS,
    )
    assert appt["status"] == "booked" and appt["participant"][1]["actor"] == {
        "reference": f"Location/{base['clinic_id']}"
    }
    assert appt["serviceType"][0]["coding"][0]["code"] == "mammography"
    alloc = fm.appointment_for_allocation(
        {
            **base,
            "id": uuid.UUID(int=31),
            "allocation_status": "proposed",
            "proposed_start_time": NOW,
            "proposed_end_time": NOW,
            "synthetic_status": "simulated",
        },
        PID,
        None,
        DS,
    )
    assert alloc["id"] == f"alloc-{uuid.UUID(int=31)}" and alloc["status"] == "proposed"
    assert alloc["participant"][0]["status"] == "needs-action" and "not offered" in alloc["description"]


def test_location_related_person_and_document() -> None:
    clinic = {
        "id": uuid.UUID(int=20),
        "clinic_key": "c1",
        "name": "Clinic 1",
        "district_code": "D01",
        "address": "1 Road",
        "timezone": "Asia/Kolkata",
        "operating_status": "open",
    }
    place = fm.location(clinic, DS)
    assert place["status"] == "active" and place["address"] == {"text": "1 Road", "district": "D01"}
    edge = {
        "id": uuid.UUID(int=40),
        "relationship_type": "mother",
        "confidence_status": "verified",
        "source_type": "deterministic_linker",
        "related_person_source_id": None,
    }
    rp = fm.related_person(edge, PID, "22222222-2222-2222-2222-222222222222", DS)
    assert rp["relationship"][0]["coding"][0]["code"] == "MTH" and rp["identifier"][0]["value"].startswith(
        "2222"
    )
    odd = fm.related_person({**edge, "relationship_type": "maternal_grandmother"}, PID, None, DS)
    assert (
        odd["relationship"][0]["coding"][0]["system"] == "urn:anticipa:relationship-type"
        and "identifier" not in odd
    )
    doc = {
        "id": uuid.UUID(int=50),
        "document_type": "lab_report",
        "language": "en",
        "content_hash": "abc",
        "created_on": NOW,
        "generation_method": "template",
        "synthetic_status": "synthetic",
    }
    ref = fm.document_reference(doc, PID, "Hemoglobin 13 g/dL", DS)
    attachment = ref["content"][0]["attachment"]
    assert attachment["size"] == len(b"Hemoglobin 13 g/dL") and "url" not in attachment
    assert (
        fm.document_reference(doc, PID, None, DS)["content"][0]["attachment"]["url"]
        == f"urn:anticipa:document:{doc['id']}"
    )


def test_enrichment_rows_become_procedure_and_observation() -> None:
    row = {
        "resource_type": "Procedure",
        "source_resource_id": "p1",
        "code_system": "http://snomed.info/sct",
        "code": "24623002",
        "display": "Screening mammography",
        "effective_start": NOW,
        "status": None,
        "value_json": None,
    }
    proc = fm.enrichment_resource(row, PID, DS)
    assert (
        proc["status"] == "completed"
        and proc["performedDateTime"] == NOW.isoformat()
        and proc["subject"]["reference"] == f"Patient/{PID}"
    )
    obs = fm.enrichment_resource(
        {
            **row,
            "resource_type": "Observation",
            "source_resource_id": "o1",
            "status": "final",
            "value_json": {
                "concept": {"system": "http://snomed.info/sct", "code": "260385009", "display": "Negative"}
            },
        },
        PID,
        DS,
    )
    assert obs["valueCodeableConcept"]["coding"][0]["code"] == "260385009" and obs["status"] == "final"
    assert fm.enrichment_resource({**row, "resource_type": "Condition"}, PID, DS) is None
