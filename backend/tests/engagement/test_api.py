"""Engagement + closure HTTP APIs (integration: DB `anticipa_test`, every test rolled back): permissions (the read-only
observer role can never mutate or trigger anything), envelopes, simulated labels and the manual loop end to end."""

from __future__ import annotations

import uuid

import pytest

API = "/api/v1"


def _post(client, path, headers, body):
    return client.post(f"{API}{path}", json=body, headers=headers)


# ------------------------------------------------------------------------------------------------ permissions
def test_observer_reads_everything_and_mutates_nothing(api_client, auth_headers, world, db_session):
    case, alloc = world.planned()
    h = auth_headers("observer")
    for path in (
        "/engagement/events", f"/care-gaps/{case.id}/communications", "/appointments", f"/care-gaps/{case.id}/completion-evidence",
        "/analytics/engagement",
    ):
        r = api_client.get(f"{API}{path}", headers=h)
        assert r.status_code == 200, (path, r.text)
        assert "request_id" in r.json()["meta"]
    assert api_client.get(f"{API}/analytics/engagement", headers=h).json()["data"]["simulated"] is True

    some = uuid.uuid4()
    mutations = [
        ("post", "/engagement/drafts", {"care_gap_case_id": str(case.id)}),
        ("post", "/engagement/events", {"care_gap_case_id": str(case.id), "channel": "sms"}),
        ("patch", f"/engagement/events/{some}/delivery-state", {"delivery_state": "sent"}),
        ("post", "/engagement/responses", {"communication_event_id": str(some), "response_type": "accept"}),
        ("post", "/engagement/simulations", {"seed": 1}),
        ("post", "/appointments", {"planned_allocation_id": str(alloc.id)}),
        ("patch", f"/appointments/{some}/status", {"appointment_status": "completed"}),
        ("post", f"/care-gaps/{case.id}/completion-evidence", {"source_type": "manual_attestation", "evidence_reference": "letter"}),
        ("post", f"/care-gaps/{case.id}/completion-evidence/{some}/review", {"decision": "verified"}),
        ("post", f"/care-gaps/{case.id}/close", {"outcome": "closed_not_completed", "reason": "n/a", "exception_type": "other"}),
    ]
    for method, path, body in mutations:
        r = api_client.request(method, f"{API}{path}", json=body, headers=h)
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", (method, path, r.text)


def test_unauthenticated_and_other_roles_are_checked(api_client, auth_headers, world, db_session):
    case, _ = world.planned()
    assert api_client.get(f"{API}/appointments").status_code == 401
    body = {"care_gap_case_id": str(case.id), "channel": "sms"}
    assert _post(api_client, "/engagement/events", auth_headers("clinical_reviewer"), body).status_code == 403  # review role cannot contact
    assert _post(api_client, "/engagement/simulations", auth_headers("care_coordinator"), {"seed": 1}).status_code == 403  # no processing.execute
    assert _post(api_client, "/engagement/simulations", auth_headers("population_admin"), {"seed": 1}).status_code == 202
    r = _post(api_client, "/engagement/events", auth_headers("system_admin"), body)
    assert r.status_code == 201, r.text
    r = _post(api_client, f"/care-gaps/{case.id}/close", auth_headers("system_admin"),
              {"outcome": "closed_not_completed", "reason": "n/a", "exception_type": "other"})  # system_admin has no caregaps.close
    assert r.status_code == 403


def test_simulation_job_submission_is_idempotent_and_validated(api_client, auth_headers, db_session):
    h = auth_headers("population_admin")
    first = _post(api_client, "/engagement/simulations", h, {"seed": 3, "limit": 10, "idempotency_key": "sim-test-1"})
    again = _post(api_client, "/engagement/simulations", h, {"seed": 3, "limit": 10, "idempotency_key": "sim-test-1"})
    assert (first.status_code, again.status_code) == (202, 200)
    assert first.json()["data"]["id"] == again.json()["data"]["id"] and first.json()["data"]["job_type"] == "engagement.simulate"
    assert _post(api_client, "/engagement/simulations", h, {"seed": 3, "rates": {"accept": 0.9, "decline": 0.5}}).status_code == 422
    assert _post(api_client, "/engagement/simulations", h, {"seed": 3, "bogus": 1}).status_code == 422


# ------------------------------------------------------------------------------------------------ the manual loop
def test_manual_loop_from_draft_to_verified_closure(api_client, auth_headers, world, db_session):
    case, alloc = world.planned("hi", status="approved_for_planning")  # planning confirmed the seat; nothing else happened
    admin, reviewer = auth_headers("population_admin"), auth_headers("clinical_reviewer")

    held = _post(api_client, "/appointments", admin, {"planned_allocation_id": str(alloc.id)})  # default: proposed
    assert held.status_code == 201
    a = held.json()["data"]
    assert a["appointment_status"] == "proposed" and a["simulated"] is True and a["clinic_key"] == world.clinic.clinic_key
    db_session.refresh(case)
    assert case.status == "planned"  # the confirmed allocation moved the case through the caregaps transition service
    dup = _post(api_client, "/appointments", admin, {"planned_allocation_id": str(alloc.id)})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "CONFLICT"

    draft =_post(api_client, "/engagement/drafts", admin, {"care_gap_case_id": str(case.id)})
    assert draft.status_code == 200
    d = draft.json()["data"]
    assert d["language"] == "hi" and d["simulated"] is True and "सिम्युलेटेड" in d["body"]

    event = _post(api_client, "/engagement/events", admin, {"care_gap_case_id": str(case.id), "channel": "sms", "delivery_state": "queued"})
    assert event.status_code == 201
    e = event.json()["data"]
    assert e["simulated"] is True and e["synthetic_status"] == "simulated" and e["language"] == "hi" and e["delivery_state"] == "queued"
    patch = api_client.patch(f"{API}/engagement/events/{e['id']}/delivery-state", json={"delivery_state": "delivered"}, headers=admin)
    assert patch.status_code == 200 and patch.json()["data"]["delivery_state"] == "delivered"
    bad = api_client.patch(f"{API}/engagement/events/{e['id']}/delivery-state", json={"delivery_state": "sent"}, headers=admin)
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "INVALID_TRANSITION"

    resp = _post(api_client, "/engagement/responses", admin, {"communication_event_id": e["id"], "response_type": "accept"})
    assert resp.status_code == 201 and resp.json()["data"]["simulated"] is True

    history = api_client.get(f"{API}/care-gaps/{case.id}/communications", headers=auth_headers("observer")).json()["data"]
    assert len(history) == 1 and history[0]["simulated"] is True and history[0]["responses"][0]["response_type"] == "accept"

    confirm = api_client.patch(f"{API}/appointments/{a['id']}/status", json={"appointment_status": "simulated_confirmed"}, headers=admin)
    assert confirm.status_code == 200 and confirm.json()["data"]["appointment_status"] == "simulated_confirmed"

    # outreach and a booked appointment alone do not close the case
    close = {"outcome": "completed", "reason": "Screening completed"}
    r = _post(api_client, f"/care-gaps/{case.id}/close", admin, close)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"

    done = api_client.patch(f"{API}/appointments/{a['id']}/status", json={"appointment_status": "completed"}, headers=admin)
    assert done.status_code == 200 and done.json()["data"]["appointment_status"] == "completed"
    r = _post(api_client, f"/care-gaps/{case.id}/close", admin, close)  # attended, still no evidence
    assert r.status_code == 422

    sub = _post(api_client, f"/care-gaps/{case.id}/completion-evidence", admin,
                {"source_type": "manual_attestation", "evidence_reference": "clinic letter (synthetic)"})  # fmt: skip
    assert sub.status_code == 201 and sub.json()["data"]["verification_status"] == "pending"
    ev = sub.json()["data"]["id"]
    assert _post(api_client, f"/care-gaps/{case.id}/close", admin, close).status_code == 422  # pending is not verified
    self_review = _post(api_client, f"/care-gaps/{case.id}/completion-evidence/{ev}/review", admin, {"decision": "verified"})
    assert self_review.status_code == 403  # the submitter cannot verify their own evidence
    reviewed = _post(api_client, f"/care-gaps/{case.id}/completion-evidence/{ev}/review", reviewer, {"decision": "verified", "note": "ok"})
    assert reviewed.status_code == 200 and reviewed.json()["data"]["verification_status"] == "verified"

    closed = _post(api_client, f"/care-gaps/{case.id}/close", admin, close)
    assert closed.status_code == 200
    c = closed.json()["data"]
    assert c["status"] == "completed" and c["evidence_id"] == ev and c["simulated"] is True
    again = _post(api_client, f"/care-gaps/{case.id}/close", admin, close)
    assert again.status_code == 409 and again.json()["error"]["code"] == "INVALID_TRANSITION"

    overview = api_client.get(f"{API}/analytics/engagement", headers=auth_headers("observer")).json()["data"]
    assert overview["cases"]["by_status"]["completed"] >= 1 and overview["funnel"]["cases_completed"] >= 1
    assert overview["appointments"]["by_status"]["completed"] >= 1 and overview["simulated"] is True


def test_past_slot_booking_is_a_conflict_over_http(api_client, auth_headers, world, db_session):
    case = world.case("approved_for_planning")
    alloc = world.allocation(case, past=True)
    r = _post(api_client, "/appointments", auth_headers("population_admin"), {"planned_allocation_id": str(alloc.id)})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONFLICT"


@pytest.mark.parametrize(("path", "body"), [
    ("/engagement/events", {"channel": "pigeon"}),
    ("/engagement/events", {"channel": "sms", "language": "fr"}),
    ("/appointments", {"appointment_status": "completed"}),
])
def test_invalid_input_is_a_validation_error(api_client, auth_headers, world, db_session, path, body):
    case, alloc = world.planned()
    payload = {"care_gap_case_id": str(case.id), "planned_allocation_id": str(alloc.id), **body}
    r = _post(api_client, path, auth_headers("population_admin"), payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_case_and_event_are_not_found(api_client, auth_headers, db_session):
    h = auth_headers("population_admin")
    assert _post(api_client, "/engagement/events", h, {"care_gap_case_id": str(uuid.uuid4()), "channel": "sms"}).status_code == 404
    assert api_client.get(f"{API}/appointments/{uuid.uuid4()}", headers=h).status_code == 404
    assert api_client.get(f"{API}/care-gaps/{uuid.uuid4()}/communications", headers=h).status_code == 404
    assert api_client.get(f"{API}/care-gaps/{uuid.uuid4()}/completion-evidence", headers=h).status_code == 404
