"""Planning API: RBAC (observer read-only), run lifecycle, confirmation re-validation, capacity overview.

The real `require_permission` dependencies run; only `get_current_user` is replaced (role taken from `x-test-role`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from anticipa.core.models import AuditEvent
from anticipa.planning import service
from anticipa.planning.models import CapacitySlot, PlannedAllocation

pytestmark = pytest.mark.integration
ADMIN, OBSERVER, COORD = {"x-test-role": "population_admin"}, {"x-test-role": "observer"}, {"x-test-role": "care_coordinator"}
PERIOD = {"start": "2026-11-10T00:00:00Z", "end": "2026-11-17T00:00:00Z"}


def run_and_wait(client, sessions, **body):
    r = client.post("/api/v1/planning/runs", json={"planning_period": PERIOD, **body}, headers=ADMIN)
    assert r.status_code == 202, r.text
    run_id = uuid.UUID(r.json()["data"]["id"])
    service.execute_run(sessions, run_id)  # stands in for the worker picking up the queued job
    return run_id


def test_clinic_registration_and_rbac(client, sessions):
    clinic = {"clinic_key": "d01-new", "name": "New clinic", "district_code": "D01",
              "services": [{"service_key": "mammography", "service_name": "Mammography", "appointment_duration_minutes": 30}]}  # fmt: skip
    r = client.post("/api/v1/planning/clinics", json=clinic, headers=ADMIN)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["synthetic_status"] == "simulated" and data["services"][0]["service_key"] == "mammography"
    assert client.post("/api/v1/planning/clinics", json=clinic, headers=ADMIN).status_code == 409
    for headers in (OBSERVER, COORD):  # observer is read-only; coordinators cannot manage capacity
        assert client.post("/api/v1/planning/clinics", json={**clinic, "clinic_key": "x"}, headers=headers).status_code == 403
    assert client.get("/api/v1/planning/clinics", headers=OBSERVER).json()["meta"]["total"] == 1
    assert client.get("/api/v1/planning/clinics?district_code=D02", headers=OBSERVER).json()["data"] == []
    with sessions() as s:
        assert s.scalar(select(AuditEvent.action).where(AuditEvent.action == "planning.clinic.create")) is not None


def test_slot_registration_validation_and_listing(client):
    client.post("/api/v1/planning/clinics", headers=ADMIN, json={
        "clinic_key": "c1", "name": "C1", "district_code": "D01",
        "services": [{"service_key": "svc", "service_name": "S", "appointment_duration_minutes": 30}]})  # fmt: skip
    slot = {"clinic_key": "c1", "service_key": "svc", "start_time": "2026-11-10T09:00:00Z", "end_time": "2026-11-10T09:30:00Z",
            "available_capacity": 4}  # fmt: skip
    r = client.post("/api/v1/planning/capacity-slots", json={"slots": [slot]}, headers=ADMIN)
    assert r.status_code == 201 and r.json()["data"]["created"] == 1
    again = client.post("/api/v1/planning/capacity-slots", json={"slots": [slot]}, headers=ADMIN).json()["data"]
    assert (again["created"], again["skipped_existing"]) == (0, 1)
    bad = [{**slot, "service_key": "nope"}, {**slot, "start_time": "2026-11-10T09:00:00"}, {**slot, "end_time": "2026-11-10T08:00:00Z"},
           {**slot, "available_capacity": -1}]  # fmt: skip
    for b in bad:
        assert client.post("/api/v1/planning/capacity-slots", json={"slots": [b]}, headers=ADMIN).status_code == 422
    assert client.post("/api/v1/planning/capacity-slots", json={"slots": [slot]}, headers=OBSERVER).status_code == 403
    listed = client.get("/api/v1/planning/capacity-slots?clinic_key=c1", headers=OBSERVER).json()
    assert listed["meta"]["total"] == 1
    row = listed["data"][0]
    assert (row["remaining_capacity"], row["synthetic_status"], row["service_key"]) == (4, "simulated", "svc")
    assert client.get("/api/v1/planning/capacity-slots?from=2026-11-10T00:00:00", headers=OBSERVER).status_code == 422


def test_run_lifecycle_through_the_api(client, sessions, seed_world):
    seed_world(sessions, 300, seats=200)
    body = {"planning_period": PERIOD, "case_filters": {"status": ["approved_for_planning", "awaiting_capacity"]},
            "objective": "feasible_capacity_allocation", "equity_constraints": {}}  # fmt: skip
    first = client.post("/api/v1/planning/runs", json=body, headers={**ADMIN, "Idempotency-Key": "k1"})
    assert first.status_code == 202 and first.json()["data"]["status"] == "queued"
    run_id = first.json()["data"]["id"]
    second = client.post("/api/v1/planning/runs", json=body, headers={**ADMIN, "Idempotency-Key": "k1"})
    assert second.status_code == 200 and second.json()["data"]["id"] == run_id  # same key -> same run
    for headers in (OBSERVER, COORD):  # triggering processing needs processing.execute
        assert client.post("/api/v1/planning/runs", json=body, headers=headers).status_code == 403
    for bad in ({"planning_period": {"start": "2026-11-10T00:00:00", "end": "2026-11-17T00:00:00Z"}},
                {"planning_period": {"start": PERIOD["end"], "end": PERIOD["start"]}},
                {"planning_period": PERIOD, "objective": "minimise_everything"},
                {"planning_period": PERIOD, "case_filters": {"status": ["completed"]}}):  # fmt: skip
        assert client.post("/api/v1/planning/runs", json=bad, headers=ADMIN).status_code == 422

    service.execute_run(sessions, uuid.UUID(run_id))
    run = client.get(f"/api/v1/planning/runs/{run_id}", headers=OBSERVER).json()["data"]
    assert run["status"] == "completed" and run["synthetic_status"] == "simulated" and run["metrics"]["hard_constraints"]["violations"] == 0
    assert run["metrics"]["allocated"] + run["metrics"]["unallocated"] == 300 and run["solver_parameters"]["seed"] == 42
    listed = client.get("/api/v1/planning/runs", headers=OBSERVER).json()
    assert listed["meta"]["total"] == 1 and "equity" not in listed["data"][0]["metrics"]  # headline metrics only in the list

    allocs = client.get(f"/api/v1/planning/runs/{run_id}/allocations?page_size=50", headers=OBSERVER).json()
    assert allocs["meta"]["total"] == run["metrics"]["allocated"] and len(allocs["data"]) == 50
    assert all(a["allocation_status"] == "proposed" and a["explanation"] for a in allocs["data"])
    assert client.get(f"/api/v1/planning/runs/{run_id}/allocations?allocation_status=confirmed", headers=OBSERVER).json()["meta"]["total"] == 0
    un = client.get(f"/api/v1/planning/runs/{run_id}/unallocated?reason_code=capacity_exhausted", headers=OBSERVER).json()
    assert un["meta"]["total"] == run["metrics"]["reason_counts"]["capacity_exhausted"]
    assert all(u["reason_code"] == "capacity_exhausted" and u["explanation"] for u in un["data"])
    assert client.get(f"/api/v1/planning/runs/{uuid.uuid4()}", headers=OBSERVER).status_code == 404
    assert client.get(f"/api/v1/planning/runs/{run_id}/unallocated?reason_code=bogus", headers=OBSERVER).status_code == 422


def test_confirmation_revalidates_the_slot(client, sessions, seed_world):
    seed_world(sessions, 150, seats=200)
    run_a = run_and_wait(client, sessions)
    run_b = run_and_wait(client, sessions)  # a second proposal for the very same cases
    with sessions() as s:
        a1, a2 = s.scalars(select(PlannedAllocation).where(PlannedAllocation.planning_run_id == run_a)
                           .order_by(PlannedAllocation.id).limit(2)).all()  # fmt: skip
        twin = s.scalar(select(PlannedAllocation).where(PlannedAllocation.planning_run_id == run_b,
                                                        PlannedAllocation.care_gap_case_id == a1.care_gap_case_id))  # fmt: skip
        slot_id = a1.capacity_slot_id
        before = s.get(CapacitySlot, slot_id).reserved_capacity
    url = "/api/v1/planning/allocations/{}/confirm"
    assert client.post(url.format(a1.id), headers=OBSERVER).status_code == 403
    r = client.post(url.format(a1.id), headers=ADMIN)
    assert r.status_code == 200, r.text
    out = r.json()["data"]
    assert out["newly_confirmed"] and out["allocation"]["allocation_status"] == "confirmed" and out["appointment_created"] is True
    with sessions() as s:
        assert s.get(CapacitySlot, slot_id).reserved_capacity == before + 1
    assert client.post(url.format(a1.id), headers=ADMIN).json()["data"]["newly_confirmed"] is False  # idempotent, no double reserve
    with sessions() as s:
        assert s.get(CapacitySlot, slot_id).reserved_capacity == before + 1
    # the same case proposed in another run is now moot
    refused = client.post(url.format(twin.id), headers=ADMIN)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "CONFLICT"
    # a slot that filled up since planning is refused
    with sessions() as s:
        s.get(CapacitySlot, a2.capacity_slot_id).reserved_capacity = s.get(CapacitySlot, a2.capacity_slot_id).available_capacity
        s.commit()
    assert client.post(url.format(a2.id), headers=ADMIN).status_code == 409
    with sessions() as s:
        outcomes = s.scalars(select(AuditEvent.outcome).where(AuditEvent.action == "planning.allocation.confirm")).all()
        assert sorted(outcomes) == ["failure", "failure", "success"]
        assert s.get(PlannedAllocation, a2.id).allocation_status == "proposed"
    assert client.post(url.format(uuid.uuid4()), headers=ADMIN).status_code == 404


def test_capacity_overview(client, sessions, seed_world):
    empty = client.get("/api/v1/analytics/capacity", headers=OBSERVER)
    assert empty.status_code == 200 and empty.json()["data"]["latest_run"] is None and empty.json()["data"]["capacity"]["total_seats"] == 0
    seed_world(sessions, 150, seats=200)
    run_id = run_and_wait(client, sessions)
    with sessions() as s:
        alloc = s.scalar(select(PlannedAllocation).where(PlannedAllocation.planning_run_id == run_id))
    assert client.post(f"/api/v1/planning/allocations/{alloc.id}/confirm", headers=ADMIN).status_code == 200
    for headers in (OBSERVER, COORD):  # analytics.view / analytics.view_limited
        r = client.get("/api/v1/analytics/capacity", headers=headers)
        assert r.status_code == 200
    d = client.get("/api/v1/analytics/capacity", headers=OBSERVER).json()["data"]
    cap = d["capacity"]
    assert d["simulated"] and d["window"]["source"] == "latest_run"
    assert cap["reserved_seats"] == 1 and cap["remaining_seats"] == cap["total_seats"] - 1 and 0 < cap["utilization"] < 1
    assert sum(r["total_seats"] for r in d["by_district"]) == cap["total_seats"]
    run = d["latest_run"]
    assert run["id"] == str(run_id) and run["allocated"] + run["unallocated"] == 150 and "reason_counts" in run
    assert sum(r["planned_seats"] for r in d["by_district"]) == run["allocated"] - 1  # one confirmed, the rest still proposed
    only = client.get("/api/v1/analytics/capacity?district_code=D01", headers=OBSERVER).json()["data"]
    assert [r["district_code"] for r in only["by_district"]] == ["D01"]
    assert client.get("/api/v1/analytics/capacity?from=2026-11-10T00:00:00Z&to=2026-11-11T00:00:00Z", headers=OBSERVER).status_code == 200
    assert isinstance(datetime.fromisoformat(d["window"]["start"].replace("Z", "+00:00")), datetime)
