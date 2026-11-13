"""API behaviour that needs the database: profile upsert, readiness, jobs routes, audit, role enforcement end-
to-end."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update

from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.core.models import UserProfile
from anticipa.jobs import service
from anticipa.jobs.models import ProcessingJob

from ..conftest import role_user_id  # noqa: TID252


def test_me_upserts_the_user_profile(api_client, auth_headers, db_session):
    for _ in range(2):  # second call updates, never duplicates
        assert api_client.get("/api/v1/auth/me", headers=auth_headers("care_coordinator")).status_code == 200
    rows = (
        db_session.execute(select(UserProfile).where(UserProfile.id == role_user_id("care_coordinator")))
        .scalars()
        .all()
    )
    assert (
        len(rows) == 1
        and rows[0].email == "care_coordinator@example.test"
        and rows[0].last_seen_at is not None
    )


def test_readiness_answers_head_for_uptime_monitors(api_client):
    resp = api_client.head("/api/v1/health/ready")
    assert resp.status_code == 200 and resp.content == b""


def test_readiness_is_ok_when_migrated(api_client):
    resp = api_client.get("/api/v1/health/ready")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert (
        data["status"] == "ready"
        and data["migrations"] == "ok"
        and data["current_revision"] == data["head_revision"]
    )


def _new_job(db_session):
    job = service.submit_job(db_session, "system.noop", {"items": 1}).job
    db_session.commit()
    return job


def test_observer_cannot_see_or_change_jobs(api_client, auth_headers, db_session):
    job = _new_job(db_session)
    headers = auth_headers("observer")  # jobs are internal backend data: system_admin only
    assert api_client.get("/api/v1/jobs", headers=headers).status_code == 403
    assert api_client.get(f"/api/v1/jobs/{job.id}", headers=headers).status_code == 403
    assert api_client.get(f"/api/v1/jobs/{job.id}/progress", headers=headers).status_code == 403
    for action in ("cancel", "retry"):
        assert api_client.post(f"/api/v1/jobs/{job.id}/{action}", headers=headers).status_code == 403
    db_session.expire_all()
    assert db_session.get(ProcessingJob, job.id).status == "queued"  # untouched
    ops = auth_headers("system_admin")
    listing = api_client.get("/api/v1/jobs", headers=ops)
    assert listing.status_code == 200 and listing.json()["meta"]["total"] >= 1
    detail = api_client.get(f"/api/v1/jobs/{job.id}", headers=ops).json()["data"]
    assert detail["id"] == str(job.id) and detail["checkpoint"] is not None  # operators see the checkpoint


def test_admin_cancel_is_audited_and_validated(api_client, auth_headers, db_session):
    job = _new_job(db_session)
    headers = auth_headers(
        "population_admin"
    )  # may cancel (processing.execute) but not browse jobs (jobs.view)
    resp = api_client.post(f"/api/v1/jobs/{job.id}/cancel", headers=headers)
    assert resp.status_code == 200 and resp.json()["data"]["status"] == "cancelled"
    events = api_client.get("/api/v1/audit", headers=headers, params={"action": "job.cancel"}).json()["data"]
    assert any(e["resource_id"] == str(job.id) and e["actor_role"] == "population_admin" for e in events)
    assert (
        api_client.post(f"/api/v1/jobs/{job.id}/cancel", headers=headers).status_code == 409
    )  # already cancelled
    assert (
        api_client.post(f"/api/v1/jobs/{uuid.uuid4()}/cancel", headers=headers).json()["error"]["code"]
        == "NOT_FOUND"
    )
    assert (
        api_client.get(f"/api/v1/jobs/{job.id}", headers=headers).status_code == 403
    )  # browsing is operator only
    ops = auth_headers("system_admin")
    assert api_client.get("/api/v1/jobs/not-a-uuid", headers=ops).status_code == 422
    assert api_client.get("/api/v1/jobs", headers=ops, params={"status": "bogus"}).status_code == 422
    assert api_client.get("/api/v1/jobs", headers=ops, params={"page_size": 1000}).status_code == 422


def test_audit_visibility_follows_role(api_client, auth_headers, db_session):
    other = uuid.uuid4()
    audit.record(
        db_session, Actor(other, "system_admin"), "demo.other", "thing", "o1", details={"note": "free text"}
    )
    audit.record(
        db_session, Actor(role_user_id("care_coordinator"), "care_coordinator"), "demo.mine", "thing", "m1"
    )
    db_session.commit()

    def actions(role):
        resp = api_client.get(
            "/api/v1/audit", headers=auth_headers(role), params={"resource_type": "thing", "page_size": 200}
        )
        return resp.status_code, {
            e["action"] for e in resp.json()["data"]
        } if resp.status_code == 200 else set()

    assert actions("population_admin") == (200, {"demo.other", "demo.mine"})
    assert actions("observer") == (403, set())  # the audit trail is internal: not for judges
    assert actions("care_coordinator") == (200, {"demo.mine"})  # scoped to own events
    assert actions("clinical_reviewer")[1] == set()
    assert (
        api_client.get(
            "/api/v1/audit", headers=auth_headers("care_coordinator"), params={"outcome": "x"}
        ).status_code
        == 422
    )


def test_claimed_job_progress_endpoint_reports_counters(api_client, auth_headers, db_session):
    job = _new_job(db_session)
    db_session.execute(
        update(ProcessingJob).where(ProcessingJob.id == job.id).values(total_items=4, processed_items=1)
    )
    db_session.commit()
    data = api_client.get(f"/api/v1/jobs/{job.id}/progress", headers=auth_headers("system_admin")).json()[
        "data"
    ]
    assert data["percent_complete"] == 25.0 and data["status"] == "queued"


def test_audit_trail_is_closed_to_observers_and_unredacted_for_admins(api_client, auth_headers, db_session):
    actor = uuid.uuid4()
    audit.record(
        db_session, Actor(actor, "system_admin"), "demo.redact", "thing", "r1", details={"note": "free text"},
        request_id="req-123",
    )  # fmt: skip
    db_session.commit()
    params = {"resource_id": "r1", "resource_type": "thing"}
    full = api_client.get("/api/v1/audit", headers=auth_headers("population_admin"), params=params).json()[
        "data"
    ]
    assert [(e["actor_id"], e["request_id"], e["details"]) for e in full] == [
        (str(actor), "req-123", {"note": "free text"})
    ]
    seen = api_client.get("/api/v1/audit", headers=auth_headers("observer"), params=params)
    assert seen.status_code == 403 and "free text" not in seen.text and str(actor) not in seen.text
