"""Care-gap HTTP API (05 §12): list/detail with the evaluation behind each case, validated transitions with
INVALID_TRANSITION
envelopes, scope and permission rules per role, escalate / reopen / assignment, and case-generation jobs.
Integration:
DB `anticipa_test`, rolled back."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from anticipa.caregaps import service

ON = date(2026, 11, 8)


@pytest.fixture
def h(seed_module):
    return seed_module


def api_case(h, seed, db_session, **resident_kw) -> dict:
    version, _ = seed.approved_version()
    resident = seed.resident(**resident_kw)
    h.run_evaluation(db_session, [version], ON, resident_ids=[resident.id])
    assert service.create_cases_scan(h.session_factory(db_session), [version.id])["created"] == 1
    from sqlalchemy import select

    from anticipa.caregaps.models import CareGapCase

    case = db_session.execute(select(CareGapCase).where(CareGapCase.resident_id == resident.id)).scalar_one()
    return {"id": str(case.id), "resident": resident, "version": version, "activity": version.care_activity}


def patch(client, headers, case_id, status, reason=None):
    return client.patch(
        f"/api/v1/care-gaps/{case_id}/status",
        headers=headers,
        json={"status": status, **({"reason": reason} if reason else {})},
    )


def test_list_filters_and_envelope(api_client, auth_headers, h, seed, db_session):
    a = api_case(h, seed, db_session, district="D7")
    b = api_case(h, seed, db_session, district="D8")
    observer = auth_headers("observer")
    resp = api_client.get(f"/api/v1/care-gaps?care_activity={a['activity']}", headers=observer)
    body = resp.json()
    assert resp.status_code == 200 and [c["id"] for c in body["data"]] == [a["id"]]
    assert body["meta"]["total"] == 1 and body["meta"]["page"] == 1 and body["meta"]["request_id"]
    assert (
        body["data"][0]["district_code"] == "D7"
        and body["data"][0]["status"] == "approved_for_planning"
        and body["data"][0]["synthetic_status"] == "synthetic"
    )
    by_district = api_client.get(
        f"/api/v1/care-gaps?district=D8&care_activity={b['activity']}", headers=observer
    ).json()
    assert [c["id"] for c in by_district["data"]] == [b["id"]]
    assert (
        api_client.get(
            f"/api/v1/care-gaps?care_activity={a['activity']}&status=planned", headers=observer
        ).json()["meta"]["total"]
        == 0
    )
    assert api_client.get("/api/v1/care-gaps?status=bogus", headers=observer).status_code == 422
    assert (
        api_client.get(
            f"/api/v1/care-gaps?care_activity={a['activity']}&unassigned=true", headers=observer
        ).json()["meta"]["total"]
        == 1
    )


def test_case_detail_shows_the_evaluation_rule_and_history_behind_it(
    api_client, auth_headers, h, seed, db_session
):
    case = api_case(h, seed, db_session)
    resp = api_client.get(f"/api/v1/care-gaps/{case['id']}", headers=auth_headers("observer"))
    assert resp.status_code == 200
    d = resp.json()["data"]
    assert (
        d["evaluation"]["outcome"] == "eligible"
        and d["evaluation"]["review_status"] == "pending_clinical_review"
    )
    assert d["evaluation"]["explanation"]["criteria"] and isinstance(
        d["evaluation"]["conflicting_information"], list
    )
    assert (
        d["rule"]["guideline_key"]
        and d["rule"]["guideline_version"] == 1
        and d["rule"]["clinical_review_status"] == "pending_clinical_review"
    )
    assert [(x["from_status"], x["to_status"]) for x in d["history"]] == [
        (None, "identified"),
        ("identified", "approved_for_planning"),
    ]
    assert d["allowed_transitions"] == sorted(
        ["awaiting_capacity", "planned", "pending_review", "escalated", "cancelled"]
    )
    assert d["resident"]["synthetic_status"] == "synthetic" and d["resident"]["id"] == str(
        case["resident"].id
    )
    assert (
        api_client.get(f"/api/v1/care-gaps/{uuid.uuid4()}", headers=auth_headers("observer")).status_code
        == 404
    )


def test_status_changes_are_validated_and_audited(api_client, auth_headers, h, seed, db_session):
    case = api_case(h, seed, db_session)
    coordinator = auth_headers("care_coordinator")
    ok = patch(api_client, coordinator, case["id"], "awaiting_capacity")
    assert ok.status_code == 200 and ok.json()["data"]["status"] == "awaiting_capacity"
    assert (
        ok.json()["data"]["history"][-1]["actor_role"] == "care_coordinator"
        and ok.json()["data"]["history"][-1]["from_status"] == "approved_for_planning"
    )
    bad = patch(api_client, coordinator, case["id"], "appointment_scheduled")
    assert (
        bad.status_code == 409
        and bad.json()["error"]["code"] == "INVALID_TRANSITION"
        and bad.json()["error"]["details"]
    )
    completed = patch(
        api_client, coordinator, case["id"], "completed"
    )  # never through the generic route: needs verified evidence
    assert completed.status_code == 409 and completed.json()["error"]["code"] == "INVALID_TRANSITION"
    assert patch(api_client, coordinator, case["id"], "cancelled").status_code == 422  # a reason is required
    assert (
        patch(api_client, coordinator, case["id"], "cancelled", "resident declined").json()["data"][
            "closure_reason"
        ]
        == "resident declined"
    )
    assert patch(api_client, coordinator, case["id"], "planned").status_code == 409  # closed: reopen instead
    audit = api_client.get(
        "/api/v1/audit?action=caregap.status_change", headers=auth_headers("population_admin")
    )
    assert audit.status_code in (
        200,
        404,
    )  # the audit API belongs to foundation; the DB-level audit is asserted in test_service


def test_permissions_per_role_on_status_changes(api_client, auth_headers, h, seed, db_session):
    case = api_case(h, seed, db_session)
    assert patch(api_client, auth_headers("observer"), case["id"], "awaiting_capacity").status_code == 403
    assert (
        patch(api_client, auth_headers("observer"), case["id"], "bogus").status_code == 403
    )  # authorisation precedes validation
    assert (
        patch(api_client, auth_headers("clinical_reviewer"), case["id"], "awaiting_capacity").status_code
        == 403
    )
    assert (
        patch(api_client, auth_headers("clinical_reviewer"), case["id"], "pending_review").status_code == 200
    )  # reviewers route to review...
    assert (
        patch(api_client, auth_headers("clinical_reviewer"), case["id"], "approved_for_planning").status_code
        == 200
    )  # ...and approve for planning
    assert (
        patch(
            api_client, auth_headers("clinical_reviewer"), case["id"], "cancelled", "not appropriate"
        ).status_code
        == 403
    )  # closing: caregaps.close
    assert patch(api_client, auth_headers("system_admin"), case["id"], "awaiting_capacity").status_code == 200


def test_coordinators_are_limited_to_their_assigned_scope(
    api_client, auth_headers, mint_token, h, seed, db_session
):
    case = api_case(h, seed, db_session)
    mine = auth_headers("care_coordinator")
    claim = api_client.patch(
        f"/api/v1/care-gaps/{case['id']}/assignment",
        headers=mine,
        json={
            "assigned_coordinator_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "care_coordinator.anticipa.test"))
        },
    )
    assert claim.status_code == 200 and claim.json()["data"]["assigned_coordinator_id"]
    other = {"Authorization": f"Bearer {mint_token('care_coordinator', sub=uuid.uuid4())}"}
    assert patch(api_client, other, case["id"], "awaiting_capacity").status_code == 403
    assert (
        api_client.patch(
            f"/api/v1/care-gaps/{case['id']}/assignment",
            headers=other,
            json={"assigned_coordinator_id": None},
        ).status_code
        == 403
    )
    assert patch(api_client, mine, case["id"], "awaiting_capacity").status_code == 200
    admin = auth_headers("population_admin")
    released = api_client.patch(
        f"/api/v1/care-gaps/{case['id']}/assignment", headers=admin, json={"assigned_coordinator_id": None}
    )
    assert released.status_code == 200 and released.json()["data"]["assigned_coordinator_id"] is None
    assert (
        api_client.patch(
            f"/api/v1/care-gaps/{case['id']}/assignment",
            headers=auth_headers("observer"),
            json={"assigned_coordinator_id": None},
        ).status_code
        == 403
    )
    assert (
        api_client.patch(f"/api/v1/care-gaps/{case['id']}/assignment", headers=admin, json={}).status_code
        == 422
    )


def test_escalate_and_reopen(api_client, auth_headers, h, seed, db_session):
    case = api_case(h, seed, db_session)
    reviewer, coordinator = auth_headers("clinical_reviewer"), auth_headers("care_coordinator")
    assert (
        api_client.post(
            f"/api/v1/care-gaps/{case['id']}/escalate", headers=coordinator, json={"reason": "x"}
        ).status_code
        == 422
    )  # reason too short
    esc = api_client.post(
        f"/api/v1/care-gaps/{case['id']}/escalate", headers=reviewer, json={"reason": "needs a clinician"}
    )
    assert esc.status_code == 200 and esc.json()["data"]["status"] == "escalated"
    assert esc.json()["data"]["history"][-1]["details"]["escalated_from"] == "approved_for_planning"
    assert (
        api_client.post(
            f"/api/v1/care-gaps/{case['id']}/reopen", headers=coordinator, json={"reason": "still open"}
        ).status_code
        == 409
    )
    assert patch(api_client, coordinator, case["id"], "cancelled", "duplicate").status_code == 200
    reopened = api_client.post(
        f"/api/v1/care-gaps/{case['id']}/reopen", headers=coordinator, json={"reason": "resident called back"}
    )
    assert (
        reopened.status_code == 200
        and reopened.json()["data"]["status"] == "pending_review"
        and reopened.json()["data"]["closed_at"] is None
    )


def test_case_generation_jobs(api_client, auth_headers, h, seed):
    version, _ = seed.approved_version()
    admin = auth_headers("population_admin")
    body = {"guideline_version_ids": [str(version.id)], "idempotency_key": f"gen-{uuid.uuid4().hex}"}
    first = api_client.post("/api/v1/care-gaps/generation-jobs", headers=admin, json=body)
    assert first.status_code == 202 and first.json()["data"]["job_type"] == "caregaps.create_cases"
    assert (
        api_client.post("/api/v1/care-gaps/generation-jobs", headers=admin, json=body).status_code == 200
    )  # idempotent
    assert (
        api_client.post(
            "/api/v1/care-gaps/generation-jobs", headers=auth_headers("observer"), json=body
        ).status_code
        == 403
    )
    assert (
        api_client.post(
            "/api/v1/care-gaps/generation-jobs", headers=admin, json={"outcomes": ["not_eligible"]}
        ).status_code
        == 422
    )
