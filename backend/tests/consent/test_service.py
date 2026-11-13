# ruff: noqa: E501  (long assertion lines)
"""Consent rules that need no database: what is in force (`evaluate`), the FHIR shape and the synthetic seed (ADR-070)."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from anticipa.consent import service
from anticipa.engagement.models import CHANNELS

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def rec(scope="outreach", status="deny", channel=None, *, days_ago=1, to=None, rid=None):
    start = NOW - timedelta(days=days_ago)
    return SimpleNamespace(
        id=rid or uuid.uuid4(), resident_id=uuid.uuid4(), scope=scope, status=status, channel=channel,
        language="en", source="coordinator_entry", effective_from=start, effective_to=to, created_at=start,
        synthetic_status="simulated",
    )  # fmt: skip


def test_no_record_means_permit():
    st = service.evaluate([], NOW)
    assert st.permitted_channels == CHANNELS and not st.outreach_blocked and not st.ai_processing_denied


def test_channel_deny_blocks_only_that_channel():
    st = service.evaluate([rec(channel="sms")], NOW)
    assert st.denied_channels == {"sms"} and "whatsapp" in st.permitted_channels and not st.outreach_blocked


def test_blanket_deny_blocks_every_channel_and_the_resident_is_unreachable():
    st = service.evaluate([rec()], NOW)
    assert st.denied_channels == set(CHANNELS) and st.outreach_blocked and st.permitted_channels == ()


def test_denying_every_channel_one_by_one_also_makes_the_resident_unreachable():
    st = service.evaluate([rec(channel=c) for c in CHANNELS], NOW)
    assert st.outreach_blocked


def test_newest_record_wins_so_a_permit_after_a_deny_reopens_the_channel():
    st = service.evaluate([rec(status="deny", channel="sms", days_ago=3), rec(status="permit", channel="sms", days_ago=1)], NOW)
    assert "sms" in st.permitted_channels
    st = service.evaluate([rec(status="permit", channel="sms", days_ago=3), rec(status="deny", channel="sms", days_ago=1)], NOW)
    assert "sms" in st.denied_channels


def test_deny_wins_across_levels_a_channel_permit_does_not_override_a_blanket_deny():
    st = service.evaluate([rec(status="deny", days_ago=3), rec(status="permit", channel="sms", days_ago=1)], NOW)
    assert st.outreach_blocked  # to reopen sms, withdraw the blanket deny


def test_a_withdrawn_blanket_deny_reopens_everything():
    st = service.evaluate([rec(status="deny", days_ago=3), rec(status="permit", days_ago=1)], NOW)
    assert st.permitted_channels == CHANNELS


def test_expired_and_future_records_are_not_in_force():
    expired = rec(days_ago=5, to=NOW - timedelta(days=1))
    future = rec(days_ago=-2)
    assert service.evaluate([expired, future], NOW).permitted_channels == CHANNELS
    assert service.evaluate([future], NOW + timedelta(days=3)).outreach_blocked  # the same record once it starts


def test_an_expired_deny_falls_back_to_the_older_permit_state():
    st = service.evaluate([rec(status="deny", channel="sms", days_ago=1, to=NOW - timedelta(hours=1))], NOW)
    assert "sms" in st.permitted_channels


def test_ai_processing_scope_is_independent_of_outreach():
    st = service.evaluate([rec(scope="data_use_ai_processing")], NOW)
    assert st.ai_processing_denied and not st.outreach_blocked and st.permitted_channels == CHANNELS
    st = service.evaluate([rec(scope="data_use_ai_processing"), rec(scope="data_use_ai_processing", status="permit", days_ago=0)], NOW)
    assert not st.ai_processing_denied


def test_in_force_lists_only_the_deciding_records():
    old, new = rec(status="deny", channel="sms", days_ago=3), rec(status="permit", channel="sms", days_ago=1)
    assert service.evaluate([old, new], NOW).in_force == (new.id,)


def test_fhir_consent_shape():
    r = rec(channel="whatsapp", to=NOW + timedelta(days=30))
    r.created_at = NOW
    fhir = service.to_fhir(r, in_force=True)
    assert fhir["resourceType"] == "Consent" and fhir["status"] == "active" and fhir["id"] == str(r.id)
    assert fhir["patient"] == {"reference": f"Patient/{r.resident_id}"} and fhir["language"] == "en"
    prov = fhir["provision"]
    assert prov["type"] == "deny" and prov["purpose"] == [{"system": service.PURPOSE_SYSTEM, "code": "outreach"}]
    assert prov["period"]["start"] == r.effective_from.isoformat() and "end" in prov["period"]
    assert prov["extension"] == [{"url": service.CHANNEL_EXTENSION, "valueCode": "whatsapp"}]
    assert fhir["meta"]["tag"][0]["code"] == "simulated"
    assert service.to_fhir(r, in_force=False)["status"] == "inactive"


def test_seed_is_deterministic_idempotent_and_roughly_two_and_a_half_percent():
    ids = [uuid.UUID(int=i * 7919) for i in range(1, 20_001)]
    rows = [r for rid in ids for r in service.seed_rows(rid, "hi", 1)]
    assert rows == [r for rid in ids for r in service.seed_rows(rid, "hi", 1)]  # same seed, same rows and ids
    assert rows != [r for rid in ids for r in service.seed_rows(rid, "hi", 2)]  # another seed, another set
    kinds = Counter(r["provenance"]["kind"] for r in rows)
    assert 60 < kinds["outreach_all"] < 140 and 240 < kinds["outreach_channel"] < 360 and 60 < kinds["ai_processing"] < 140
    assert len({r["id"] for r in rows}) == len(rows)
    assert all(r["source"] == "synthetic_seed" and r["synthetic_status"] == "synthetic" and r["status"] == "deny" for r in rows)
    assert all((r["channel"] is None) == (r["provenance"]["kind"] != "outreach_channel") for r in rows)
    assert all(r["channel"] in (None, "sms", "whatsapp", "phone_call") and r["language"] == "hi" for r in rows)
    assert all(r["scope"] == ("data_use_ai_processing" if r["provenance"]["kind"] == "ai_processing" else "outreach") for r in rows)
    assert all(r["effective_from"] <= NOW for r in rows)  # in force from the start
