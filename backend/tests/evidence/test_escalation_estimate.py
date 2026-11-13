# ruff: noqa: E501  (scripted model replies and long fixtures)
"""Deterministic escalation triggers, the 10% hard cap, and the pre-run cost estimates."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from anticipa.ai.config import load_config
from anticipa.ai.testing import FakeAdapter, make_test_router, ok
from anticipa.evidence import DocumentMeta, extract_document
from anticipa.evidence.escalation import (
    EscalationBudget,
    conflicting_family_facts,
    escalation_triggers,
    flag_conflicts_for_review,
)
from anticipa.evidence.estimate import (
    escalation_cap,
    estimate_embedding,
    estimate_extraction,
    estimate_tokens,
)

DOC = (
    "Mother was diagnosed with breast cancer at age 45. Patient denies tobacco use. "
    "Sister had breast cancer at 50. There is no family history of breast cancer. A relative had diabetes."
)
VALUE = {
    "concept": "x",
    "original_text": None,
    "detail": None,
    "numeric_value": None,
    "unit": None,
    "event_date": None,
}


def fact(quote, concept, **kw):
    base = {
        "category": "family_history", "fact_type": concept.replace(" ", "_"), "value": {**VALUE, "concept": concept},
        "subject_type": "family_member", "relationship": "mother", "assertion_status": "positive",
        "temporal_status": "historical", "age_at_onset": None, "source_quote": quote,
        "extraction_confidence": "high", "uncertainty_note": None,
    }  # fmt: skip
    return {**base, **kw}


async def run(*facts) -> object:
    router, _, _ = make_test_router(
        {"anthropic": FakeAdapter(ok(json.dumps({"document_language": "en", "facts": list(facts)})))}
    )
    return await extract_document(router, DOC, DocumentMeta(document_id="d"))


GOOD = fact("Mother was diagnosed with breast cancer at age 45.", "breast cancer", age_at_onset=45)


@pytest.mark.anyio
async def test_a_clean_result_does_not_escalate():
    assert escalation_triggers(await run(GOOD)) == []
    assert escalation_triggers(await run()) == []  # "no facts" is a valid, clean result


@pytest.mark.anyio
async def test_each_trigger_fires_on_its_own_and_only_then():
    unverified = fact("Mother had liver cancer.", "liver cancer")
    assert escalation_triggers(await run(GOOD, unverified)) == ["unverified_quote"]
    low = fact("Patient denies tobacco use.", "tobacco use", category="risk_factor", subject_type="resident",
               relationship=None, assertion_status="negative", extraction_confidence="low")  # fmt: skip
    assert escalation_triggers(await run(low)) == ["low_extraction_confidence"]
    no_rel = fact("Mother was diagnosed with breast cancer at age 45.", "breast cancer", relationship=None)
    assert escalation_triggers(await run(no_rel)) == ["ambiguous_relative_terms"]
    vague = fact("A relative had diabetes.", "diabetes", relationship="other_relative")
    assert escalation_triggers(await run(vague)) == ["ambiguous_relative_terms"]
    blanket_no = fact(
        "There is no family history of breast cancer.",
        "breast cancer",
        relationship="other_relative",
        assertion_status="negative",
    )
    assert "ambiguous_relative_terms" not in escalation_triggers(
        await run(blanket_no)
    )  # a blanket denial is clear


@pytest.mark.anyio
async def test_conflicting_family_history_is_detected_three_ways():
    mother_yes = GOOD
    mother_no = fact(
        "Mother was diagnosed with breast cancer at age 45.", "breast cancer", assertion_status="negative"
    )
    assert escalation_triggers(await run(mother_yes, mother_no)) == ["conflicting_family_history"]
    blanket_no = fact(
        "There is no family history of breast cancer.",
        "breast cancer",
        relationship="other_relative",
        assertion_status="negative",
    )
    assert "conflicting_family_history" in escalation_triggers(
        await run(mother_yes, blanket_no)
    )  # positive vs blanket negative
    age_a = fact("Mother was diagnosed with breast cancer at age 45.", "breast cancer", age_at_onset=45)
    age_b = fact("Sister had breast cancer at 50.", "breast cancer", relationship="sister", age_at_onset=50)
    assert not conflicting_family_facts(await run(age_a, age_b))  # different relatives, different ages: fine
    age_c = fact("Mother was diagnosed with breast cancer at age 45.", "breast cancer", age_at_onset=52)
    assert conflicting_family_facts(await run(age_a, age_c))  # one relative, two ages


@pytest.mark.anyio
async def test_failed_extractions_never_trigger_escalation():
    router, _, _ = make_test_router({"anthropic": FakeAdapter(ok("not json"), ok("not json"))})
    out = await extract_document(router, DOC, DocumentMeta(document_id="d"))
    assert out.status == "failed" and escalation_triggers(out) == []


def test_the_hard_cap_is_ten_percent_of_the_cohort_and_spans_earlier_runs():
    assert escalation_cap(10_000) == 1000 and escalation_cap(1_000, already_escalated=40) == 60
    assert escalation_cap(15) == 1 and escalation_cap(9) == 0 and escalation_cap(100, 500) == 0
    budget = EscalationBudget(2, {"earlier"})
    assert budget.try_take("earlier")  # already counted: resuming it takes no new slot
    assert budget.try_take("a") and not budget.try_take("b") and budget.try_take("a")
    assert budget.denied == 1 and len(budget.used) == 2
    assert not EscalationBudget(0).try_take("x")


# --------------------------------------------------------------------------------------------- estimates
def test_extraction_estimate_for_the_10k_cohort_with_haiku_and_capped_sonnet_escalation():
    cfg = load_config()
    est = estimate_extraction(cfg, 10_000, escalation_cap(10_000), run_budget=Decimal("40"))
    assert est["max_escalations"] == 1000
    assert Decimal(est["expected_haiku_usd"]) == Decimal(
        "3.9100"
    )  # 10k docs x $0.000391 (profile calibrated on 18 real docgen documents)
    assert Decimal(est["expected_sonnet_usd"]) == Decimal(
        "26.9070"
    )  # 1000 docs x $0.026907 (profile calibrated on 8 escalated documents)
    assert Decimal(est["expected_total_usd"]) == Decimal("30.8170") and est["within_run_budget"] is True
    assert Decimal(est["per_1000_documents_usd"]) == Decimal("3.0817")
    assert Decimal(est["upper_total_usd"]) > Decimal(est["expected_total_usd"])
    without = estimate_extraction(cfg, 10_000, 0)  # escalation off: Haiku only
    assert Decimal(without["expected_total_usd"]) == Decimal("3.9100")
    assert estimate_extraction(cfg, 30_000, 3000, run_budget=Decimal("40"))["within_run_budget"] is False
    # the real docgen population has ~3.2 documents per resident, so a 10,000-resident cohort is ~32,100 documents
    big = estimate_extraction(cfg, 32_100, escalation_cap(32_100), run_budget=Decimal("40"))
    assert Decimal(big["expected_total_usd"]) == Decimal("98.9226") and big["within_run_budget"] is False


def test_token_estimate_is_conservative_against_the_live_measurement():
    en = "Synthetic clinical note. The patient is a 52-year-old woman attending a routine review. Her mother was diagnosed with breast cancer at age 45. The patient denies tobacco use. Colonoscopy was advised for next year."
    hi = "कृत्रिम नैदानिक नोट। मरीज़ 52 वर्ष की महिला हैं। उनकी माँ को 45 वर्ष की आयु में स्तन कैंसर का निदान हुआ था। मरीज़ को मधुमेह नहीं है।"
    assert (
        estimate_tokens(en) + estimate_tokens(hi) >= 172
    )  # Nebius billed 172 tokens for exactly these two texts
    assert estimate_tokens("") == 0 and estimate_tokens("ab") >= 1


def test_embedding_estimate_prices_the_actual_texts():
    cfg = load_config()
    texts = ["x" * 3000, "माँ को स्तन कैंसर हुआ था। " * 40]
    est = estimate_embedding(cfg, texts)
    assert est["provider"] == "nebius" and est["dimensions"] == 1024 and est["texts"] == 2
    assert est["estimated_tokens"] == sum(estimate_tokens(t) for t in texts)
    assert Decimal(est["estimated_cost_usd"]) == (
        Decimal(est["estimated_tokens"]) * Decimal("0.01") / Decimal(1_000_000)
    ).quantize(Decimal("0.000001"), "ROUND_UP")
    assert estimate_embedding(cfg, [])["estimated_cost_usd"] == "0.000000"
    assert Decimal(estimate_embedding(cfg, ["a"])["estimated_cost_usd"]) == Decimal(
        "0.000001"
    )  # never rounds to zero


def test_the_approved_10k_run_is_2_percent_escalation_about_30_dollars_inside_the_35_dollar_cap():
    cfg = load_config()
    docs = 32_100  # 10,000 residents x ~3.21 documents per resident in the docgen population
    cap = escalation_cap(docs, 0, Decimal("0.02"))
    est = estimate_extraction(cfg, docs, cap, run_budget=Decimal("35"))
    assert cap == 642 and est["max_escalations"] == 642
    assert Decimal(est["expected_total_usd"]) == Decimal("29.8254") and est["within_run_budget"] is True
    assert escalation_cap(docs, 0, Decimal("0.50")) == 3210  # a larger share is clamped to the 10% ceiling
    assert escalation_cap(docs, 100, Decimal("0.02")) == 542  # earlier escalations count against the cap


@pytest.mark.anyio
async def test_unescalated_conflicts_are_moved_to_the_review_queue_not_left_accepted():
    mother_yes = fact("Mother was diagnosed with breast cancer at age 45.", "breast cancer", age_at_onset=45)
    mother_no = fact(
        "Mother was diagnosed with breast cancer at age 45.", "breast cancer", assertion_status="negative"
    )
    fine = fact("Patient denies tobacco use.", "tobacco use", category="risk_factor", subject_type="resident",
                relationship=None, assertion_status="negative")  # fmt: skip
    out = await run(mother_yes, mother_no, fine)
    assert [f.decision for f in out.facts] == [
        "accepted",
        "accepted",
        "accepted",
    ]  # Haiku accepted both sides
    assert flag_conflicts_for_review(out) == 2
    assert [f.decision for f in out.facts] == ["requires_review", "requires_review", "accepted"]
    assert (
        all("conflicting_family_history" in f.reasons for f in out.facts[:2]) and out.facts[2].reasons == []
    )
    assert flag_conflicts_for_review(out) == 0  # idempotent
