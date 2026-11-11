"""Deterministic quote verification (07 §7.3) and the acceptance policy (docs/17 §6), en/hi/ar."""

from __future__ import annotations

import pytest

from anticipa.evidence.policy import decide
from anticipa.evidence.quotes import verify_quote
from anticipa.evidence.schemas import EvidenceCandidate, EvidenceValue

EN = "Mother was diagnosed with breast cancer at age 45. Patient denies tobacco use."
HI = "माँ को 45 वर्ष की आयु में स्तन कैंसर का निदान हुआ था। मरीज़ को मधुमेह नहीं है।"
AR = "تم تشخيص الأم بسرطان الثدي في سن 45. لا يوجد تاريخ عائلي لسرطان القولون."


# --------------------------------------------------------------------------------------------- quotes
@pytest.mark.parametrize(
    ("doc", "quote"),
    [
        (EN, "Mother was diagnosed with breast cancer at age 45."),
        (HI, "माँ को 45 वर्ष की आयु में स्तन कैंसर का निदान हुआ था।"),
        (AR, "تم تشخيص الأم بسرطان الثدي في سن 45."),
    ],
)
def test_exact_quote_gives_exact_offsets_in_every_script(doc, quote):
    v = verify_quote(doc, quote)
    assert v.status == "verified_exact" and v.verified and not v.location_ambiguous
    ((start, end),) = v.spans
    assert doc[start:end] == quote and v.span == (start, end)


def test_quote_in_several_places_is_ambiguous_and_records_every_span():
    doc = "No fever. Cough for two days. No fever."
    v = verify_quote(doc, "No fever.")
    assert v.status == "verified_exact" and v.location_ambiguous and v.span is None
    assert [doc[a:b] for a, b in v.spans] == ["No fever.", "No fever."] and len(v.spans) == 2


def test_whitespace_and_invisible_characters_normalise_back_to_original_offsets():
    doc = "Patient  denies\ntobacco use.​ Next sentence."
    v = verify_quote(doc, "Patient denies tobacco use.")
    assert v.status == "verified_normalized" and v.method == "normalized"
    ((start, end),) = v.spans
    assert doc[start:end].startswith("Patient  denies") and doc[start:end].endswith(
        "use."
    )  # original wording


def test_unicode_composition_differences_verify_as_normalized():
    # U+095B (precomposed ज़) is canonically U+091C U+093C; the model may return either form.
    doc = "मरीज़ को बुखार है।"
    quote = "मरीज़ को बुखार है।".replace("ज़", "ज़")
    assert quote != doc
    assert verify_quote(doc, quote).status == "verified_normalized"


@pytest.mark.parametrize(
    ("doc", "quote"),
    [
        (EN, "Mother was diagnosed with ovarian cancer at age 45."),  # fabricated concept
        (EN, "mother was diagnosed with breast cancer at age 45."),  # case is never folded
        (EN, "Mother diagnosed breast cancer age 45"),  # paraphrase / elision
        (HI, "माँ को स्तन कैंसर का निदान हुआ था।"),  # elided Hindi
        (AR, "تم تشخيص الأم بسرطان الثدي في سن 54."),  # changed digit
        (AR, "تُم تشخيص الأم بسرطان الثدي في سن 45."),  # added diacritic: Arabic marks are NOT folded
    ],
)
def test_paraphrase_or_edit_is_not_found(doc, quote):
    assert verify_quote(doc, quote).status == "quote_not_found"


def test_degenerate_inputs_never_raise_and_are_never_verified():
    assert verify_quote("", "x").status == "source_missing"
    assert verify_quote("   ", "x").status == "source_missing"
    assert verify_quote(EN, "").status == "quote_not_found"
    assert verify_quote(EN, "   ").status == "quote_not_found"
    assert verify_quote(EN, None).status == "verification_error"  # type: ignore[arg-type]
    assert verify_quote(None, "x").status == "verification_error"  # type: ignore[arg-type]
    assert not any(verify_quote(EN, q).verified for q in ("", " ", "zzz"))


def test_expected_span_mismatch_is_not_verified():
    quote = "Patient denies tobacco use."
    start = EN.index(quote)
    assert verify_quote(EN, quote, expected_span=(start, start + len(quote))).status == "verified_exact"
    v = verify_quote(EN, quote, expected_span=(0, len(quote)))
    assert v.status == "span_mismatch" and not v.verified


# --------------------------------------------------------------------------------------------- policy
def cand(**kw) -> EvidenceCandidate:
    base = {
        "category": "family_history",
        "fact_type": "breast_cancer",
        "value": EvidenceValue(
            concept="breast cancer",
            original_text=None,
            detail=None,
            numeric_value=None,
            unit=None,
            event_date=None,
        ),
        "subject_type": "family_member",
        "relationship": "mother",
        "assertion_status": "positive",
        "temporal_status": "historical",
        "age_at_onset": 45,
        "source_quote": "Mother was diagnosed with breast cancer at age 45.",
        "extraction_confidence": "high",
        "uncertainty_note": None,
    }
    return EvidenceCandidate(**{**base, **kw})


def verdict(c: EvidenceCandidate, doc: str = EN):
    return decide(c, verify_quote(doc, c.source_quote))


def test_consistent_verified_fact_is_accepted():
    v = verdict(cand())
    assert v.decision == "accepted" and v.reasons == ()


def test_unverifiable_quote_is_rejected_not_dropped():
    v = verdict(cand(source_quote="Mother had liver cancer."))
    assert v.decision == "rejected" and v.reasons == ("quote_not_found",)


def test_empty_concept_is_rejected():
    c = cand(
        value=EvidenceValue(
            concept=" ", original_text=None, detail=None, numeric_value=None, unit=None, event_date=None
        )
    )
    assert verdict(c).decision == "rejected"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"subject_type": "unknown", "relationship": None, "category": "condition"}, "unknown_subject"),
        ({"relationship": None}, "family_member_without_relationship"),
        ({"subject_type": "resident", "relationship": "mother"}, "resident_with_relationship"),
        ({"subject_type": "resident", "relationship": None}, "quote_mentions_relative"),
        ({"subject_type": "other", "relationship": None, "category": "condition"}, None),  # a neighbour: fine
        ({"extraction_confidence": "low"}, "low_extraction_confidence"),
        ({"age_at_onset": 450}, "implausible_age"),
        (
            {"category": "family_history", "subject_type": "resident", "relationship": None},
            "family_history_not_family_member",
        ),
    ],
)
def test_inconsistent_or_uncertain_facts_go_to_review(change, reason):
    v = verdict(cand(**change))
    if reason is None:
        assert v.decision == "accepted"
    else:
        assert v.decision == "requires_review" and reason in v.reasons


@pytest.mark.parametrize(
    ("doc", "quote"),
    [
        (HI, "माँ को 45 वर्ष की आयु में स्तन कैंसर का निदान हुआ था।"),
        (AR, "تم تشخيص الأم بسرطان الثدي في سن 45."),
    ],
)
def test_resident_subject_on_a_relatives_quote_is_flagged_in_hindi_and_arabic(doc, quote):
    v = verdict(
        cand(subject_type="resident", relationship=None, category="condition", source_quote=quote), doc
    )
    assert v.decision == "requires_review" and "quote_mentions_relative" in v.reasons


def test_ambiguous_location_and_inconclusive_verification_require_review():
    doc = "No fever. Cough. No fever."
    c = cand(
        category="symptom",
        subject_type="resident",
        relationship=None,
        age_at_onset=None,
        source_quote="No fever.",
    )
    assert verdict(c, doc).decision == "requires_review" and "location_ambiguous" in verdict(c, doc).reasons
    mismatch = decide(c, verify_quote(doc, "No fever.", expected_span=(2, 5)))
    assert mismatch.decision == "requires_review" and "span_mismatch" in mismatch.reasons
