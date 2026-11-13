# ruff: noqa: E501  (long assertion lines)
"""Consent guards in engagement (ADR-070; integration: DB `anticipa_test`, every test rolled back): a denied channel is
refused with a 409 and an audit event, the draft offers only permitted channels, and the simulation skips or re-routes."""

from __future__ import annotations

from sqlalchemy import select

from anticipa.consent import service as consent
from anticipa.core.models import AuditEvent
from anticipa.engagement.models import CommunicationEvent
from anticipa.engagement.simulate import SimScope, plan_outcome, simulate_case

API = "/api/v1"


def _deny(session, world_module, resident_id, channel=None, *, scope="outreach"):
    consent.record_consent(session, resident_id, scope=scope, status="deny", channel=channel, actor=world_module.ADMIN)


def _audit(session, action, resident_id):
    return session.execute(
        select(AuditEvent).where(AuditEvent.action == action, AuditEvent.resource_id == str(resident_id))
    ).scalars().all()


def test_outreach_on_a_denied_channel_is_a_409_with_an_audit_event_and_writes_nothing(api_client, auth_headers, world, db_session, world_module):
    case, _ = world.planned()
    _deny(db_session, world_module, case.resident_id, "sms")
    h = auth_headers("care_coordinator")
    r = api_client.post(f"{API}/engagement/events", json={"care_gap_case_id": str(case.id), "channel": "sms"}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONFLICT", r.text
    assert r.json()["error"]["details"] == [{"field": "channel", "reason": "consent_denied"}]
    assert "consent_denied" in r.json()["error"]["message"]
    assert db_session.execute(select(CommunicationEvent).where(CommunicationEvent.care_gap_case_id == case.id)).first() is None
    db_session.refresh(case)
    assert case.status == "planned"  # the refusal left the case untouched
    (event,) = _audit(db_session, "consent.outreach.blocked", case.resident_id)
    assert event.outcome == "denied" and event.details["channel"] == "sms" and event.details["care_gap_case_id"] == str(case.id)
    assert event.actor_role == "care_coordinator"
    ok = api_client.post(f"{API}/engagement/events", json={"care_gap_case_id": str(case.id), "channel": "whatsapp"}, headers=h)
    assert ok.status_code == 201 and ok.json()["data"]["channel"] == "whatsapp"  # other channels stay open


def test_a_blanket_opt_out_blocks_every_channel(api_client, auth_headers, world, db_session, world_module):
    case, _ = world.planned()
    _deny(db_session, world_module, case.resident_id)
    for channel in ("sms", "whatsapp", "phone_call", "email"):
        r = api_client.post(f"{API}/engagement/events", json={"care_gap_case_id": str(case.id), "channel": channel}, headers=auth_headers("care_coordinator"))
        assert r.status_code == 409, channel
    assert len(_audit(db_session, "consent.outreach.blocked", case.resident_id)) == 4


def test_the_draft_offers_only_permitted_channels(api_client, auth_headers, world, db_session, world_module):
    case, _ = world.planned()
    h = auth_headers("care_coordinator")
    body = {"care_gap_case_id": str(case.id)}
    assert api_client.post(f"{API}/engagement/drafts", json=body, headers=h).json()["data"]["blocked_channels"] == []
    _deny(db_session, world_module, case.resident_id, "phone_call")
    data = api_client.post(f"{API}/engagement/drafts", json=body, headers=h).json()["data"]
    assert data["channels"] == ["sms", "whatsapp", "email"] and data["blocked_channels"] == ["phone_call"]


def test_opting_back_in_reopens_the_channel(api_client, auth_headers, world, db_session, world_module):
    case, _ = world.planned()
    _deny(db_session, world_module, case.resident_id, "sms")
    consent.record_consent(db_session, case.resident_id, scope="outreach", status="permit", channel="sms", actor=world_module.ADMIN)
    r = api_client.post(f"{API}/engagement/events", json={"care_gap_case_id": str(case.id), "channel": "sms"}, headers=auth_headers("care_coordinator"))
    assert r.status_code == 201


def test_ai_processing_opt_out_does_not_block_outreach(api_client, auth_headers, world, db_session, world_module):
    case, _ = world.planned()
    _deny(db_session, world_module, case.resident_id, scope="data_use_ai_processing")
    r = api_client.post(f"{API}/engagement/events", json={"care_gap_case_id": str(case.id), "channel": "sms"}, headers=auth_headers("care_coordinator"))
    assert r.status_code == 201


def test_simulation_skips_an_unreachable_resident_and_reserves_no_seat(world, db_session, world_module):
    case, alloc = world.planned(confirm=False)
    _deny(db_session, world_module, case.resident_id)
    scope = SimScope(seed=3, planning_run_id=world.run.id)
    assert simulate_case(db_session, case.id, alloc.id, scope, world_module.SYSTEM) is None
    db_session.refresh(alloc)
    assert alloc.allocation_status == "proposed"  # not confirmed: no seat was taken for someone who cannot be contacted
    assert db_session.execute(select(CommunicationEvent).where(CommunicationEvent.care_gap_case_id == case.id)).first() is None
    (event,) = _audit(db_session, "consent.outreach.blocked", case.resident_id)
    assert event.outcome == "denied" and event.details["stage"] == "simulation"


def test_simulation_moves_to_a_permitted_channel_when_the_drawn_one_is_opted_out(world, db_session, world_module):
    case, alloc = world.planned()
    seed = next(s for s in range(1, 200) if plan_outcome(s, case.id).channel == "sms")  # a seed that would use SMS
    _deny(db_session, world_module, case.resident_id, "sms")
    scope = SimScope(seed=seed, planning_run_id=world.run.id)
    outcome = simulate_case(db_session, case.id, alloc.id, scope, world_module.SYSTEM)
    assert outcome is not None and outcome.channel in ("whatsapp", "phone_call")
    (event,) = db_session.execute(select(CommunicationEvent).where(CommunicationEvent.care_gap_case_id == case.id)).scalars().all()
    assert event.channel == outcome.channel and event.channel != "sms"
    expected = plan_outcome(seed, case.id)
    assert (outcome.delivered, outcome.response, outcome.attendance) == (expected.delivered, expected.response, expected.attendance)  # only the channel changed
