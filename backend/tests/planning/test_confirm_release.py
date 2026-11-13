"""Confirm hardening and seat release: past slots, the engagement hold in the same transaction, release semantics."""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from anticipa.core.audit import Actor
from anticipa.core.errors import Conflict
from anticipa.core.models import AuditEvent
from anticipa.planning import service
from anticipa.planning.adapters import load_plannable_cases
from anticipa.planning.models import CapacitySlot, PlannedAllocation

pytestmark = pytest.mark.integration
ADMIN, OBSERVER, COORD = {"x-test-role": "population_admin"}, {"x-test-role": "observer"}, {"x-test-role": "care_coordinator"}
PERIOD = {"start": "2026-11-10T00:00:00Z", "end": "2026-11-17T00:00:00Z"}
CONFIRM, RELEASE = "/api/v1/planning/allocations/{}/confirm", "/api/v1/planning/allocations/{}/release"


def planned(client, sessions, seed_world, n=120):
    """One completed run; returns (run_id, first proposed allocation)."""
    seed_world(sessions, n, seats=200)
    r = client.post("/api/v1/planning/runs", json={"planning_period": PERIOD}, headers=ADMIN)
    run_id = uuid.UUID(r.json()["data"]["id"])
    service.execute_run(sessions, run_id)
    with sessions() as s:
        alloc = s.scalars(select(PlannedAllocation).where(PlannedAllocation.planning_run_id == run_id)
                          .order_by(PlannedAllocation.id).limit(1)).one()  # fmt: skip
        s.expunge(alloc)
    return run_id, alloc


def reserved(sessions, slot_id) -> int:
    with sessions() as s:
        return s.get(CapacitySlot, slot_id).reserved_capacity


def audit_rows(sessions, action):
    with sessions() as s:
        return [(e.outcome, e.details) for e in s.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.occurred_at))]


# ------------------------------------------------------------------------------------------------ past slots
def test_confirm_rejects_a_slot_that_has_already_started(client, sessions, seed_world, monkeypatch):
    _, alloc = planned(client, sessions, seed_world)
    monkeypatch.setattr(service, "_utcnow", lambda: datetime(2026, 11, 20, tzinfo=UTC))  # after the whole horizon
    r = client.post(CONFIRM.format(alloc.id), headers=ADMIN)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONFLICT" and "already started" in r.json()["error"]["message"]
    assert reserved(sessions, alloc.capacity_slot_id) == 0
    with sessions() as s:
        assert s.get(PlannedAllocation, alloc.id).allocation_status == "proposed"
    assert [o for o, _ in audit_rows(sessions, "planning.allocation.confirm")] == ["failure"]


def test_confirm_accepts_a_slot_that_starts_later_but_not_one_starting_now(client, sessions, seed_world, monkeypatch):
    _, alloc = planned(client, sessions, seed_world)
    monkeypatch.setattr(service, "_utcnow", lambda: alloc.proposed_start_time)  # start <= now is already started
    assert client.post(CONFIRM.format(alloc.id), headers=ADMIN).status_code == 409
    monkeypatch.setattr(service, "_utcnow", lambda: alloc.proposed_start_time.replace(year=2026, month=11, day=9))
    assert client.post(CONFIRM.format(alloc.id), headers=ADMIN).status_code == 200


# ------------------------------------------------------------------------------------------------ engagement hold
def test_confirm_holds_the_slot_through_engagement_in_the_same_transaction(client, sessions, seed_world, hold_calls):
    _, alloc = planned(client, sessions, seed_world)
    r = client.post(CONFIRM.format(alloc.id), headers=ADMIN)
    assert r.status_code == 200 and r.json()["data"]["appointment_created"] is True
    (call,) = hold_calls
    assert call["allocation_id"] == alloc.id and call["actor"] == Actor(uuid.uuid5(uuid.NAMESPACE_DNS, "population_admin"), "population_admin")
    assert call["status_seen"] == "confirmed"  # the hook ran after the seat was reserved, on the very same session
    assert call["user"].role == "population_admin" and call["source"] == "manual"
    again = client.post(CONFIRM.format(alloc.id), headers=ADMIN).json()["data"]
    assert again["newly_confirmed"] is False and again["appointment_created"] is False and len(hold_calls) == 1  # idempotent
    assert audit_rows(sessions, "planning.allocation.confirm")[0][1]["appointment_hold_created"] is True


def test_hold_is_passed_the_callers_source_and_job(sessions, seed_world, new_run, hold_calls):
    seed_world(sessions, 60, seats=100)
    run_id = new_run(sessions)
    service.execute_run(sessions, run_id)
    job_id = uuid.uuid4()
    with sessions() as s:
        alloc = s.scalars(select(PlannedAllocation).where(PlannedAllocation.planning_run_id == run_id)).first()
        result = service.confirm_allocation(s, alloc.id, Actor.system(), source="simulation", job_id=job_id)
        s.commit()
    assert result.newly_confirmed and result.appointment_created
    assert hold_calls[0]["source"] == "simulation"


def test_a_refused_hold_rolls_back_the_reservation(client, sessions, seed_world, monkeypatch):
    _, alloc = planned(client, sessions, seed_world)
    import anticipa.engagement.service as engagement

    def refuse(*args, **kwargs):
        raise Conflict("The case is closed; reopen it first.")

    monkeypatch.setattr(engagement, "on_allocation_confirmed", refuse)
    r = client.post(CONFIRM.format(alloc.id), headers=ADMIN)
    assert r.status_code == 409 and "appointment hold was refused" in r.json()["error"]["message"]
    assert reserved(sessions, alloc.capacity_slot_id) == 0  # nothing stays reserved
    with sessions() as s:
        assert s.get(PlannedAllocation, alloc.id).allocation_status == "proposed"
    (outcome, details), = audit_rows(sessions, "planning.allocation.confirm")
    assert outcome == "failure" and details["stage"] == "appointment_hold"


def test_confirm_still_works_when_engagement_cannot_be_imported(client, sessions, seed_world, monkeypatch, caplog):
    _, alloc = planned(client, sessions, seed_world)
    monkeypatch.setitem(sys.modules, "anticipa.engagement.service", None)  # `import` now raises ImportError
    with caplog.at_level(logging.WARNING, logger="anticipa.planning.service"):
        r = client.post(CONFIRM.format(alloc.id), headers=ADMIN)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["newly_confirmed"] is True and data["appointment_created"] is False
    assert reserved(sessions, alloc.capacity_slot_id) == 1
    assert any("engagement module unavailable" in m for m in caplog.messages)


# ------------------------------------------------------------------------------------------------ release
def test_release_gives_the_seat_back_and_is_idempotent_and_audited(client, sessions, seed_world):
    _, alloc = planned(client, sessions, seed_world)
    assert client.post(CONFIRM.format(alloc.id), headers=ADMIN).status_code == 200
    assert reserved(sessions, alloc.capacity_slot_id) == 1
    r = client.post(RELEASE.format(alloc.id), json={"reason": "Appointment cancelled (simulated)"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["released"] is True and data["allocation"]["allocation_status"] == "superseded" and data["synthetic_status"] == "simulated"
    assert reserved(sessions, alloc.capacity_slot_id) == 0
    again = client.post(RELEASE.format(alloc.id), headers=ADMIN)  # no body at all
    assert again.status_code == 200 and again.json()["data"]["released"] is False and reserved(sessions, alloc.capacity_slot_id) == 0
    (outcome, details), = audit_rows(sessions, "planning.allocation.release")
    assert outcome == "success" and details["reason"] == "Appointment cancelled (simulated)"
    assert details["capacity_slot_id"] == str(alloc.capacity_slot_id)
    assert client.post(CONFIRM.format(alloc.id), headers=ADMIN).status_code == 409  # a released allocation cannot come back


def test_released_case_can_be_planned_again_and_the_seat_reused(client, sessions, seed_world):
    run_id, alloc = planned(client, sessions, seed_world)
    client.post(CONFIRM.format(alloc.id), headers=ADMIN)
    with sessions() as s:
        assert alloc.care_gap_case_id not in {c.case_id for c in load_plannable_cases(s)}  # confirmed: not planned again
    client.post(RELEASE.format(alloc.id), headers=ADMIN)
    with sessions() as s:
        assert alloc.care_gap_case_id in {c.case_id for c in load_plannable_cases(s)}  # released: plannable again
    r = client.post("/api/v1/planning/runs", json={"planning_period": PERIOD}, headers=ADMIN)
    second = uuid.UUID(r.json()["data"]["id"])
    service.execute_run(sessions, second)
    with sessions() as s:
        assert s.scalar(select(PlannedAllocation.id).where(PlannedAllocation.planning_run_id == second,
                                                           PlannedAllocation.care_gap_case_id == alloc.care_gap_case_id))  # fmt: skip


def test_release_refuses_unconfirmed_and_unknown_allocations_and_non_managers(client, sessions, seed_world):
    _, alloc = planned(client, sessions, seed_world)
    refused = client.post(RELEASE.format(alloc.id), headers=ADMIN)  # still only a proposal
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "CONFLICT"
    assert client.post(RELEASE.format(uuid.uuid4()), headers=ADMIN).status_code == 404
    client.post(CONFIRM.format(alloc.id), headers=ADMIN)
    for headers in (OBSERVER, COORD):
        assert client.post(RELEASE.format(alloc.id), headers=headers).status_code == 403
    assert reserved(sessions, alloc.capacity_slot_id) == 1  # the refused calls changed nothing
    assert client.post(RELEASE.format(alloc.id), json={"reason": "x" * 301}, headers=ADMIN).status_code == 422
    assert client.post(RELEASE.format(alloc.id), headers={"x-test-role": "system_admin"}).status_code == 200


def test_reserved_capacity_never_goes_negative_on_release(sessions, seed_world, new_run):
    seed_world(sessions, 40, seats=60)
    run_id = new_run(sessions)
    service.execute_run(sessions, run_id)
    with sessions() as s:
        alloc = s.scalars(select(PlannedAllocation).where(PlannedAllocation.planning_run_id == run_id)).first()
        service.confirm_allocation(s, alloc.id, Actor.system())
        s.get(CapacitySlot, alloc.capacity_slot_id).reserved_capacity = 0  # drifted data: seat already given back elsewhere
        s.commit()
        released, did = service.release_allocation(s, alloc.id, Actor.system())
        s.commit()
        assert did and released.allocation_status == "superseded" and s.get(CapacitySlot, alloc.capacity_slot_id).reserved_capacity == 0


def test_hold_can_be_skipped_for_callers_that_book_themselves(sessions, seed_world, new_run, hold_calls):
    seed_world(sessions, 40, seats=60)
    run_id = new_run(sessions)
    service.execute_run(sessions, run_id)
    with sessions() as s:
        alloc = s.scalars(select(PlannedAllocation).where(PlannedAllocation.planning_run_id == run_id)).first()
        result = service.confirm_allocation(s, alloc.id, Actor.system(), hold_appointment=False)
        s.commit()
    assert result.newly_confirmed and not result.appointment_created and hold_calls == []
