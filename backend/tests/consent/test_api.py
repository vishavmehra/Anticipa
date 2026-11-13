# ruff: noqa: E501  (long assertion lines)
"""Consent HTTP API (integration: DB `anticipa_test`, every test rolled back): permissions, validation, append-only
semantics, what is in force, audit and the synthetic seed (ADR-070)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from anticipa.consent import service
from anticipa.consent.models import ConsentRecord
from anticipa.core.audit import Actor
from anticipa.core.models import AuditEvent

API = "/api/v1"


def url(resident) -> str:
    return f"{API}/residents/{resident.id}/consents"


def post(client, headers, resident, **body):
    return client.post(url(resident), json=body, headers=headers)


def audit_actions(db_session, resident) -> list[tuple[str, str]]:
    rows = db_session.execute(
        select(AuditEvent.action, AuditEvent.outcome).where(AuditEvent.resource_id.in_([str(resident.id)]))
    ).all()
    return [(a, o) for a, o in rows]


# ------------------------------------------------------------------------------------------------ permissions
def test_every_role_reads_but_only_coordinator_and_admin_record(api_client, auth_headers, resident):
    for role in ("observer", "clinical_reviewer", "system_admin", "care_coordinator", "population_admin"):
        r = api_client.get(url(resident), headers=auth_headers(role))
        assert r.status_code == 200, (role, r.text)
        assert r.json()["data"]["effective"]["outreach"]["blocked"] is False
    body = {"scope": "outreach", "status": "deny", "channel": "sms"}
    for role in ("observer", "clinical_reviewer", "system_admin"):
        r = api_client.post(url(resident), json=body, headers=auth_headers(role))
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", role
    for role in ("care_coordinator", "population_admin"):
        assert api_client.post(url(resident), json=body, headers=auth_headers(role)).status_code == 201, role
    assert api_client.get(url(resident)).status_code == 401
    assert api_client.post(url(resident), json=body).status_code == 401


# ------------------------------------------------------------------------------------------------ record and read
def test_recording_a_deny_shows_up_in_force_with_fhir_and_is_audited(api_client, auth_headers, resident, db_session):
    h = auth_headers("care_coordinator")
    r = post(api_client, h, resident, scope="outreach", status="deny", channel="sms", language="hi",
             source="resident_request", note="asked at the clinic desk")  # fmt: skip
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["effective"]["outreach"] == {
        "permitted_channels": ["whatsapp", "phone_call", "email"], "denied_channels": ["sms"], "blocked": False,
    }  # fmt: skip
    assert data["effective"]["data_use_ai_processing"] == "permit"
    (rec,) = data["records"]
    assert rec["scope"] == "outreach" and rec["status"] == "deny" and rec["channel"] == "sms" and rec["in_force"] is True
    assert rec["source"] == "resident_request" and rec["language"] == "hi" and rec["note"] == "asked at the clinic desk"
    assert rec["synthetic_status"] == "simulated" and rec["recorded_by"] is not None
    assert "provenance" not in rec and "request_id" not in str(rec["note"])  # provenance is not exposed, only the note
    assert rec["fhir"]["resourceType"] == "Consent" and rec["fhir"]["provision"]["type"] == "deny"
    assert rec["fhir"]["patient"]["reference"] == f"Patient/{resident.id}" and rec["fhir"]["status"] == "active"
    assert "synthetic" in data["notice"].lower()
    stored = db_session.execute(select(ConsentRecord).where(ConsentRecord.resident_id == resident.id)).scalar_one()
    assert stored.provenance["actor_role"] == "care_coordinator" and stored.provenance["note"]
    event = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "consent.record", AuditEvent.resource_id == str(stored.id))
    ).scalar_one()
    assert event.outcome == "success" and event.details["scope"] == "outreach" and event.details["resident_id"] == str(resident.id)
    assert "asked at the clinic desk" not in str(event.details)  # the free-text note stays out of the audit trail
    api_client.get(url(resident), headers=auth_headers("observer"))
    assert ("resident.consent.read", "success") in audit_actions(db_session, resident)


def test_withdrawing_is_recording_the_opposite_status_and_history_is_kept(api_client, auth_headers, resident):
    h = auth_headers("population_admin")
    assert post(api_client, h, resident, scope="outreach", status="deny").status_code == 201  # blanket opt-out
    state = api_client.get(url(resident), headers=h).json()["data"]
    assert state["effective"]["outreach"]["blocked"] is True and state["effective"]["outreach"]["permitted_channels"] == []
    assert post(api_client, h, resident, scope="outreach", status="permit").status_code == 201  # withdrawn
    state = api_client.get(url(resident), headers=h).json()["data"]
    assert state["effective"]["outreach"]["blocked"] is False
    assert [(x["status"], x["in_force"]) for x in state["records"]] == [("permit", True), ("deny", False)]  # newest first
    assert state["records"][1]["fhir"]["status"] == "inactive"


def test_ai_processing_scope(api_client, auth_headers, resident):
    h = auth_headers("care_coordinator")
    data = post(api_client, h, resident, scope="data_use_ai_processing", status="deny").json()["data"]
    assert data["effective"]["data_use_ai_processing"] == "deny" and data["effective"]["outreach"]["blocked"] is False


def test_a_time_limited_decision_is_accepted_and_reported(api_client, auth_headers, resident):
    r = post(api_client, auth_headers("care_coordinator"), resident, scope="outreach", status="deny", channel="email",
             effective_to="2099-01-01T00:00:00Z")  # fmt: skip
    assert r.status_code == 201 and r.json()["data"]["records"][0]["effective_to"].startswith("2099-01-01")


# ------------------------------------------------------------------------------------------------ validation
def test_invalid_requests_are_rejected_and_write_nothing(api_client, auth_headers, resident, db_session):
    h = auth_headers("care_coordinator")
    bad = [
        {"scope": "outreach", "status": "maybe"},
        {"scope": "marketing", "status": "deny"},
        {"scope": "data_use_ai_processing", "status": "deny", "channel": "sms"},  # a channel is for outreach only
        {"scope": "outreach", "status": "deny", "channel": "pigeon"},
        {"scope": "outreach", "status": "deny", "language": "fr"},
        {"scope": "outreach", "status": "deny", "source": "synthetic_seed"},  # the seed is not an API input
        {"scope": "outreach", "status": "deny", "effective_to": "2001-01-01T00:00:00Z"},
        {"scope": "outreach", "status": "deny", "effective_to": "2099-01-01T00:00:00"},  # naive timestamp
        {"scope": "outreach", "status": "deny", "note": "x" * 301},
        {"scope": "outreach", "status": "deny", "extra": 1},
        {"status": "deny"},
    ]
    for body in bad:
        r = post(api_client, h, resident, **body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR", body
    assert db_session.execute(select(ConsentRecord)).first() is None


def test_unknown_resident_is_404(api_client, auth_headers):
    ghost = f"{API}/residents/{uuid.uuid4()}/consents"
    assert api_client.get(ghost, headers=auth_headers("observer")).status_code == 404
    r = api_client.post(ghost, json={"scope": "outreach", "status": "deny"}, headers=auth_headers("care_coordinator"))
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


# ------------------------------------------------------------------------------------------------ service level
def test_unreachable_residents_lists_only_blanket_or_all_channel_denials(db_session, residents):
    a, b, c, d = residents.make(4)
    actor = Actor(uuid.uuid4(), "care_coordinator")
    service.record_consent(db_session, a.id, scope="outreach", status="deny", actor=actor)
    service.record_consent(db_session, b.id, scope="outreach", status="deny", channel="sms", actor=actor)
    for ch in ("sms", "whatsapp", "phone_call", "email"):
        service.record_consent(db_session, c.id, scope="outreach", status="deny", channel=ch, actor=actor)
    service.record_consent(db_session, d.id, scope="data_use_ai_processing", status="deny", actor=actor)
    assert service.unreachable_residents(db_session) == {a.id, c.id}
    service.record_consent(db_session, a.id, scope="outreach", status="permit", actor=actor)
    assert service.unreachable_residents(db_session) == {c.id}


def test_seed_inserts_a_small_deterministic_set_is_idempotent_and_removable(db_session, residents):
    ids = [uuid.uuid5(uuid.NAMESPACE_URL, f"consent-seed-test:{i}") for i in range(1500)]
    residents.make(1500, ids=ids)
    version = residents.dataset.dataset_version
    dry = service.seed_synthetic_optouts(db_session, dataset_version=version, dry_run=True)
    assert dry["residents"] == 1500 and dry["inserted"] == 0
    assert db_session.execute(select(ConsentRecord)).first() is None  # a dry run writes nothing
    first = service.seed_synthetic_optouts(db_session, dataset_version=version)
    total = first["outreach_all"] + first["outreach_channel"] + first["ai_processing"]
    assert 10 < total < 80 and first["inserted"] == total  # about 2.5% of 1500
    again = service.seed_synthetic_optouts(db_session, dataset_version=version)
    assert again["inserted"] == 0  # derived ids: re-running adds nothing
    rows = db_session.execute(select(ConsentRecord)).scalars().all()
    assert all(r.source == "synthetic_seed" and r.synthetic_status == "synthetic" and r.status == "deny" for r in rows)
    unreachable = service.unreachable_residents(db_session)
    assert len(unreachable) == first["outreach_all"]  # only the blanket opt-outs cannot be reached at all
    person = service.record_consent(db_session, ids[0], scope="outreach", status="permit", actor=Actor(uuid.uuid4(), "care_coordinator"))
    assert service.remove_seeded(db_session) == total
    assert db_session.execute(select(ConsentRecord)).scalars().all() == [person]  # a person's decision is never removed
