"""Resident conversation over HTTP (integration: DB `anticipa_test`, every test rolled back, provider scripted): thread,
escalation through the case lifecycle, opt out into the consent ledger, permissions, limits and the audit trail."""
# ruff: noqa: E501

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from conv_samples import Script, step
from sqlalchemy import select

from anticipa.ai.settings import AiSettings
from anticipa.ai.testing import err, make_test_router
from anticipa.caregaps.models import CareGapCase, CaseStatusHistory
from anticipa.consent import service as consent
from anticipa.consent.models import ConsentRecord
from anticipa.conversation import safety, service
from anticipa.conversation.models import ConversationMessage
from anticipa.core.audit import Actor
from anticipa.core.models import AuditEvent
from anticipa.engagement import service as engagement

API = "/api/v1"
NO_DIGITS = step("scheduling", "A care coordinator will confirm the date and the clinic with you.")


@pytest.fixture
def provider(monkeypatch):
    """Route every model call to a scripted provider; `provider.script` can be replaced per test."""

    class P:
        script = Script(NO_DIGITS)

    monkeypatch.setattr(service, "get_router", lambda: make_test_router({"anthropic": P.script})[0])
    return P


def post(client, headers, case_id, text, language=None, channel=None):
    body = {"text": text, **({"language": language} if language else {}), **({"channel": channel} if channel else {})}
    return client.post(f"{API}/care-gaps/{case_id}/conversation/messages", json=body, headers=headers)


def thread(client, headers, case_id):
    r = client.get(f"{API}/care-gaps/{case_id}/conversation", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def status_of(db_session, case):
    db_session.expire_all()  # the API ran in its own session on the same transaction
    return db_session.get(CareGapCase, case.id).status


COORDINATOR = Actor(uuid.UUID("22222222-2222-4222-8222-222222222222"), "care_coordinator")


def deny(db_session, case, *, scope="outreach", channel=None):
    consent.record_consent(db_session, case.resident_id, scope=scope, status="deny", actor=COORDINATOR, channel=channel, source="coordinator_entry")
    db_session.flush()


def audits_of(db_session, action, resource_id):
    return list(db_session.execute(select(AuditEvent).where(AuditEvent.action == action, AuditEvent.resource_id == str(resource_id))).scalars())


def audits(db_session, case, action):
    return [e for e in db_session.execute(select(AuditEvent).where(AuditEvent.action == action, AuditEvent.resource_id == str(case.id))).scalars()]


def test_a_logistics_message_gets_a_grounded_reply_and_both_sides_are_stored_as_simulated(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    for _i in range(2):  # open slots of the same service: the alternatives the assistant may propose
        world.allocation(world.case("planned"), confirm=False)
    h = auth_headers("population_admin")
    r = post(api_client, h, case.id, "Can I come on a different day?")
    assert r.status_code == 201, r.text
    d = r.json()["data"]
    assert d["escalation"] is None and d["suppressed"] is False and d["simulated"] is True and d["case_status"] == "planned"
    assert d["resident_message"]["role"] == "resident" and d["resident_message"]["simulated"] is True and d["resident_message"]["marker"] is None
    assert d["reply"]["role"] == "assistant" and d["reply"]["source"] == "model" and d["reply"]["marker"] == safety.SIMULATION_MARKERS["en"]
    assert d["reply"]["synthetic_status"] == "simulated" and d["cost_usd"] > 0
    user_turn = provider.script.calls[0][1][0]  # the facts the model saw came from this case
    assert "Test clinic (simulated)" in user_turn and '"ref":"s1"' in user_turn and "Friday 20 November 2026" in user_turn
    t = thread(api_client, h, case.id)
    assert [m["role"] for m in t["messages"]] == ["resident", "assistant"] and t["escalated"] is False and t["simulated"] is True


def test_a_medical_question_is_never_answered_and_escalates_the_case_through_the_lifecycle(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    h = auth_headers("population_admin")
    r = post(api_client, h, case.id, "I found a lump in my breast, what should I do?")
    d = r.json()["data"]
    assert r.status_code == 201 and d["reply"]["body"] == safety.approved("clinician", "en") and d["reply"]["source"] == "template"
    assert d["escalation"] == {"reason": "medical_question", "state": "escalated", "urgent": False} and d["case_status"] == "escalated"
    assert provider.script.calls == []  # the model never saw it
    assert status_of(db_session, case) == "escalated"
    hist = db_session.execute(select(CaseStatusHistory).where(CaseStatusHistory.case_id == case.id, CaseStatusHistory.to_status == "escalated")).scalars().one()
    assert "medical question" in hist.reason and "lump" not in hist.reason  # the reason names the category, never the resident's words
    assert d["resident_message"]["escalation_state"] == "escalated" and d["resident_message"]["escalation_reason"] == "medical_question"
    assert thread(api_client, h, case.id)["escalated"] is True


def test_an_emergency_gets_the_urgent_text_and_a_second_escalation_does_not_move_the_case_again(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    h = auth_headers("population_admin")
    first = post(api_client, h, case.id, "I have severe chest pain and I can't breathe").json()["data"]
    assert first["escalation"] == {"reason": "emergency", "state": "escalated", "urgent": True}
    assert "nearest hospital" in first["reply"]["body"] and "cannot give medical advice" in first["reply"]["body"]
    second = post(api_client, h, case.id, "Do I have cancer?").json()["data"]
    assert second["escalation"]["state"] == "already_escalated" and second["case_status"] == "escalated"
    assert db_session.execute(select(CaseStatusHistory).where(CaseStatusHistory.case_id == case.id, CaseStatusHistory.to_status == "escalated")).scalars().all().__len__() == 1


def test_a_closed_case_cannot_be_escalated_but_the_request_is_still_recorded_and_audited(api_client, auth_headers, world, provider, db_session):
    case = world.case("completed")
    r = post(api_client, auth_headers("population_admin"), case.id, "I have a pain in my stomach")
    d = r.json()["data"]
    assert r.status_code == 201 and d["escalation"]["state"] == "case_closed" and d["case_status"] == "completed"
    assert status_of(db_session, case) == "completed"
    assert audits(db_session, case, "conversation.escalation")[0].details["state"] == "case_closed"


def test_eligibility_dispute_goes_to_a_person_without_a_model_answer(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    d = post(api_client, auth_headers("population_admin"), case.id, "I already had this test last year at another clinic").json()["data"]
    assert d["escalation"]["reason"] == "eligibility_dispute" and d["reply"]["body"] == safety.approved("review", "en") and provider.script.calls == []


def test_hindi_and_arabic_messages_get_replies_in_their_language(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned("hi")
    h = auth_headers("population_admin")
    hi = post(api_client, h, case.id, "मेरे सीने में तेज दर्द है").json()["data"]
    assert hi["reply"]["language"] == "hi" and hi["reply"]["direction"] == "ltr" and hi["reply"]["body"] == safety.approved("emergency", "hi")
    case2, _ = world.planned("ar")
    ar = post(api_client, h, case2.id, "عندي كتلة في الثدي").json()["data"]
    assert ar["reply"]["language"] == "ar" and ar["reply"]["direction"] == "rtl" and ar["reply"]["marker"] == safety.SIMULATION_MARKERS["ar"]


def test_an_opt_out_creates_a_deny_record_through_the_consent_service(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    h = auth_headers("population_admin")
    d = post(api_client, h, case.id, "Please stop sending me messages").json()["data"]
    assert d["opt_out"]["detected"] is True and d["opt_out"]["consent_recorded"] is True and d["escalation"] is None
    assert d["reply"]["body"] == safety.approved("opt_out", "en") and d["suppressed"] is False  # the acknowledgement goes out, the channel was still open
    db_session.expire_all()
    rec = db_session.execute(select(ConsentRecord).where(ConsentRecord.resident_id == case.resident_id)).scalars().one()
    assert str(rec.id) == d["opt_out"]["consent_record_id"]
    assert (rec.scope, rec.status, rec.channel, rec.source, rec.synthetic_status, rec.language) == ("outreach", "deny", None, "resident_request", "simulated", "en")
    assert rec.provenance["actor_role"] == "population_admin" and "resident conversation" in rec.provenance["note"]
    assert audits_of(db_session, "consent.record", rec.id)[0].details["source"] == "resident_request"  # the consent service wrote its own audit event
    assert audits(db_session, case, "conversation.opt_out")[0].details["consent_record_id"] == str(rec.id)
    state = consent.state_for(db_session, case.resident_id)
    assert state.outreach_blocked and set(state.denied_channels) == {"sms", "whatsapp", "phone_call", "email"}


def test_after_an_opt_out_no_further_reply_is_sent_on_any_channel(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    h = auth_headers("population_admin")
    post(api_client, h, case.id, "STOP")
    quiet = post(api_client, h, case.id, "When is my appointment?", channel="whatsapp").json()["data"]
    assert quiet["suppressed"] is True and quiet["suppressed_reason"] == "consent_denied" and quiet["reply"] is None and quiet["channel"] == "whatsapp"
    t = thread(api_client, h, case.id)
    assert t["consent"]["outreach_blocked"] is True and [m["role"] for m in t["messages"]] == ["resident", "assistant", "resident"]


def test_a_denied_channel_gets_no_reply_but_the_message_is_recorded_audited_and_a_clinician_is_still_alerted(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    deny(db_session, case, channel="sms")
    h = auth_headers("population_admin")
    d = post(api_client, h, case.id, "When is my appointment?", channel="sms").json()["data"]
    assert d["suppressed"] is True and d["suppressed_reason"] == "consent_denied" and d["reply"] is None and d["channel"] == "sms"
    assert d["resident_message"]["reply_sent"] is False and d["resident_message"]["role"] == "resident"
    blocked = audits_of(db_session, "consent.outreach.blocked", case.resident_id)
    assert len(blocked) == 1 and blocked[0].outcome == "denied" and blocked[0].details["channel"] == "sms" and blocked[0].details["care_gap_case_id"] == str(case.id)
    msg = audits(db_session, case, "conversation.message")[0].details
    assert msg["suppressed"] is True and msg["suppressed_reason"] == "consent_denied" and msg["channel"] == "sms"
    t = thread(api_client, h, case.id)
    assert t["consent"]["denied_channels"] == ["sms"] and t["consent"]["outreach_blocked"] is False
    assert [(m["role"], m["reply_sent"]) for m in t["messages"]] == [("resident", False)]
    # a health matter on the denied channel: nothing is sent to the resident, but the internal hand off is never lost
    med = post(api_client, h, case.id, "Do I have cancer?", channel="sms").json()["data"]
    assert med["suppressed"] is True and med["reply"] is None and med["escalation"]["state"] == "escalated" and med["case_status"] == "escalated"
    # a channel the resident did not deny is unaffected
    other = post(api_client, h, case.id, "When is my appointment?", channel="whatsapp").json()["data"]
    assert other["suppressed"] is False and other["reply"]["source"] == "model" and other["channel"] == "whatsapp"


def test_an_emergency_always_gets_the_urgent_reply_even_on_a_denied_channel_and_the_audit_notes_the_override(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    deny(db_session, case)  # blanket: every outreach channel is denied
    h = auth_headers("population_admin")
    d = post(api_client, h, case.id, "I have severe chest pain and I can't breathe", channel="sms").json()["data"]
    assert d["suppressed"] is False and d["suppressed_reason"] is None and d["consent_override"] == "emergency_safety_reply"
    assert d["reply"]["body"] == safety.approved("emergency", "en") and d["reply"]["source"] == "template" and d["resident_message"]["reply_sent"] is True
    assert d["escalation"] == {"reason": "emergency", "state": "escalated", "urgent": True} and d["case_status"] == "escalated"
    assert provider.script.calls == []
    rows = db_session.execute(select(ConversationMessage).where(ConversationMessage.care_gap_case_id == case.id)).scalars().all()
    assert sorted(r.role for r in rows) == ["assistant", "resident"]  # the urgent reply is stored, not only returned
    msg = audits(db_session, case, "conversation.message")[0].details
    assert msg["consent_override"] == "emergency_safety_reply" and msg["suppressed"] is False and msg["escalation"] == "emergency" and msg["channel"] == "sms"
    assert len(audits_of(db_session, "consent.outreach.blocked", case.resident_id)) == 1  # the denial itself was still checked and audited
    assert audits(db_session, case, "conversation.escalation")[0].details["reason"] == "emergency"
    # the override is for emergencies only: a medical question on the same denied channel is escalated but not answered
    med = post(api_client, h, case.id, "Do I have cancer?", channel="sms").json()["data"]
    assert med["suppressed"] is True and med["reply"] is None and med["consent_override"] is None and med["escalation"]["reason"] == "medical_question"
    assert [m["reply_sent"] for m in thread(api_client, h, case.id)["messages"] if m["role"] == "resident"] == [True, False]


def test_the_channel_defaults_to_the_last_one_used_on_the_case(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    engagement.record_event(db_session, case, channel="whatsapp", delivery_state="delivered", actor=Actor(COORDINATOR.id, "population_admin"))
    db_session.flush()
    deny(db_session, case, channel="sms")  # sms is denied, whatsapp (the channel of the last message) is not
    h = auth_headers("population_admin")
    d = post(api_client, h, case.id, "When is my appointment?").json()["data"]
    assert d["channel"] == "whatsapp" and d["suppressed"] is False and d["reply"] is not None
    deny(db_session, case, channel="whatsapp")
    assert post(api_client, h, case.id, "When is my appointment?").json()["data"]["suppressed"] is True


def test_without_any_history_the_reply_is_checked_against_sms(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    deny(db_session, case, channel="sms")
    d = post(api_client, auth_headers("population_admin"), case.id, "When is my appointment?").json()["data"]
    assert d["channel"] == "sms" and d["suppressed"] is True


def test_a_resident_who_denied_ai_processing_is_never_sent_to_the_model(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    deny(db_session, case, scope="data_use_ai_processing")
    h = auth_headers("population_admin")
    d = post(api_client, h, case.id, "When is my appointment?").json()["data"]
    assert d["reply"]["body"] == safety.approved("unavailable", "en") and d["suppressed"] is False and provider.script.calls == []
    assert post(api_client, h, case.id, "I have chest pain").json()["data"]["escalation"]["urgent"] is True  # the keyword layer needs no model


def test_an_unknown_channel_is_rejected(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    assert post(api_client, auth_headers("population_admin"), case.id, "hello", channel="pigeon").status_code == 422


def test_the_observer_reads_the_thread_and_cannot_post(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    post(api_client, auth_headers("population_admin"), case.id, "When is my appointment?")
    assert thread(api_client, auth_headers("observer"), case.id)["messages"]
    r = post(api_client, auth_headers("observer"), case.id, "hello there")
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    assert post(api_client, auth_headers("clinical_reviewer"), case.id, "hello there").status_code == 403
    assert api_client.get(f"{API}/care-gaps/{case.id}/conversation").status_code == 401
    assert post(api_client, auth_headers("system_admin"), case.id, "When is my appointment?").status_code == 201


def test_a_coordinator_cannot_post_on_a_case_assigned_to_someone_else(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    case.assigned_coordinator_id = uuid.uuid4()
    db_session.flush()
    assert post(api_client, auth_headers("care_coordinator"), case.id, "When is my appointment?").status_code == 403


def test_unknown_cases_and_bad_bodies_are_rejected(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    h = auth_headers("population_admin")
    assert post(api_client, h, uuid.uuid4(), "hello").status_code == 404
    assert post(api_client, h, case.id, "").status_code == 422
    assert post(api_client, h, case.id, "x" * 601).status_code == 422
    assert post(api_client, h, case.id, "hello", language="fr").status_code == 422
    assert api_client.post(f"{API}/care-gaps/{case.id}/conversation/messages", json={"text": "hi", "extra": 1}, headers=h).status_code == 422


def test_the_per_case_message_limit_returns_429(api_client, auth_headers, world, provider, monkeypatch, db_session):
    monkeypatch.setattr(service, "MAX_RESIDENT_MESSAGES_PER_CASE", 2)
    case, _ = world.planned()
    h = auth_headers("population_admin")
    assert [post(api_client, h, case.id, "I have chest pain").status_code for _ in range(2)] == [201, 201]
    r = post(api_client, h, case.id, "I have chest pain")
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"


def test_over_the_daily_model_budget_the_model_is_skipped_but_safety_still_works(api_client, auth_headers, world, provider, monkeypatch, db_session):
    monkeypatch.setattr(service, "get_ai_settings", lambda: AiSettings(_env_file=None, anticipa_converse_daily_budget_usd=Decimal("0")))
    case, _ = world.planned()
    h = auth_headers("population_admin")
    d = post(api_client, h, case.id, "When is my appointment?").json()["data"]
    assert d["reply"]["body"] == safety.approved("unavailable", "en") and provider.script.calls == []
    assert post(api_client, h, case.id, "I have chest pain").json()["data"]["escalation"]["urgent"] is True


def test_a_provider_outage_gives_the_approved_text_and_no_escalation(api_client, auth_headers, world, provider, db_session):
    provider.script = Script(err("unavailable"))
    case, _ = world.planned()
    d = post(api_client, auth_headers("population_admin"), case.id, "When is my appointment?").json()["data"]
    assert d["reply"]["body"] == safety.approved("unavailable", "en") and d["reply"]["source"] == "template" and d["escalation"] is None
    assert status_of(db_session, case) == "planned"


def test_the_audit_trail_has_a_hash_and_the_decision_never_the_resident_text(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    secret = "I found a lump in my breast, what should I do?"
    post(api_client, auth_headers("population_admin"), case.id, secret)
    ev = audits(db_session, case, "conversation.message")
    assert len(ev) == 1
    details = ev[0].details
    assert details["intent"] == "medical" and details["escalation"] == "medical_question" and details["simulated"] is True and len(details["message_hash"]) == 16
    assert "lump" not in str(details) and secret not in str(details)
    assert audits(db_session, case, "caregap.status_change") and audits(db_session, case, "conversation.escalation")


def test_messages_are_kept_in_order_and_resident_text_is_stored_verbatim_but_normalised_for_spaces(api_client, auth_headers, world, provider, db_session):
    case, _ = world.planned()
    h = auth_headers("population_admin")
    post(api_client, h, case.id, "When   is my   appointment?")
    post(api_client, h, case.id, "ok thanks")
    rows = db_session.execute(select(ConversationMessage).where(ConversationMessage.care_gap_case_id == case.id)).scalars().all()
    assert len(rows) == 4 and {r.synthetic_status for r in rows} == {"simulated"}
    t = thread(api_client, h, case.id)["messages"]
    assert [m["role"] for m in t] == ["resident", "assistant", "resident", "assistant"] and t[0]["body"] == "When is my appointment?"
