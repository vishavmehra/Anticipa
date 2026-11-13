# ruff: noqa: E501
"""Deterministic post-extraction normaliser n1: category from measurement shape, temporal status from dates."""

from __future__ import annotations

from datetime import date

import pytest

from anticipa.evidence.normalise import (
    NORMALISER_VERSION,
    apply_to_value,
    is_measurement,
    normalise,
    originals,
    parse_event_date,
)

DOC = date(2026, 3, 14)


def val(**kw):
    base = {"concept": "x", "original_text": None, "detail": None, "numeric_value": None, "unit": None, "event_date": None}
    return {**base, **kw}


@pytest.mark.parametrize(
    ("value", "fact_type", "expected"),
    [
        (val(concept="blood pressure", detail="128/82", unit="mmHg"), "blood_pressure", True),
        (val(concept="heart rate", numeric_value=72.0, unit="bpm"), "heart_rate", True),
        (val(concept="BMI", numeric_value=31.2, unit="kg/m2"), "bmi", True),
        (val(concept="fasting glucose", numeric_value=7.4, unit="mmol/L"), "glucose", True),
        (val(concept="LDL cholesterol"), "ldl", True),  # named lab even without a value or unit
        (val(concept="oxygen saturation", numeric_value=97, unit="%"), "spo2", True),
        (val(concept="chest x-ray showing consolidation"), "chest_xray", False),  # an interpretation, not a result
        (val(concept="ECG with sinus rhythm"), "ecg", False),
        (val(concept="mass in the left breast"), "breast_mass", False),
    ],
)
def test_measurement_shape(value, fact_type, expected):
    assert is_measurement(value, fact_type) is expected


def test_c1_moves_lab_and_vital_results_out_of_diagnostic_finding_only():
    n = normalise("diagnostic_finding", "current", "positive", "hba1c", val(concept="HbA1c", numeric_value=53, unit="mmol/mol"), DOC)
    assert (n.category, n.rules) == ("observation", ["C1:measurement_shape"])
    keep = normalise("diagnostic_finding", "current", "positive", "chest_xray", val(concept="chest x-ray: consolidation"), DOC)
    assert keep.category == "diagnostic_finding" and not keep.changed
    other = normalise("condition", "current", "positive", "hypertension", val(concept="hypertension", numeric_value=150, unit="mmHg"), DOC)
    assert other.category == "condition"  # only diagnostic_finding is re-filed
    assert normalise("observation", "current", "positive", "glucose", val(numeric_value=7.4, unit="mmol/L"), DOC).category == "observation"


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("2026-03-14", None),  # same day as the document: current
        ("2026-03-10", None),  # within the 7-day tolerance
        ("2026-03-01", "historical"),
        ("2021-06-15", "historical"),
        ("2026-02", "historical"),  # an earlier month
        ("2026-03", None),  # the same month: not clearly different
        ("2024", "historical"),
        ("2026", None),
        ("March 2025", "historical"),
        ("12 March 2024", "historical"),
        ("March 5, 2025", "historical"),
        ("some years ago", None),  # not an exact date: never guessed
        ("कुछ साल पहले", None),
        ("12 मार्च 2024", None),  # non-English month names are not parsed
        (None, None),
    ],
)
def test_t1_dates_before_the_document_make_a_completed_event_historical(event, expected):
    n = normalise("procedure", "current", "positive", "colonoscopy", val(event_date=event), DOC)
    assert (n.temporal_status == "historical") is (expected == "historical")
    assert n.category == "procedure"
    assert n.rules == (["T1:event_before_document"] if expected else [])


def test_t1_future_dates_plan_procedures_but_not_results():
    assert normalise("procedure", "current", "positive", "colonoscopy", val(event_date="2026-09-01"), DOC).temporal_status == "planned"
    assert normalise("preventive_care", "unknown", "positive", "mammography", val(event_date="2027"), DOC).temporal_status == "planned"
    assert normalise("observation", "current", "positive", "glucose", val(event_date="2026-09-01"), DOC).temporal_status == "current"


@pytest.mark.parametrize(
    ("category", "temporal", "assertion"),
    [
        ("condition", "current", "positive"),  # a diagnosis made long ago may be ongoing
        ("medication", "current", "positive"),
        ("family_history", "current", "positive"),
        ("procedure", "historical", "positive"),  # the model already said historical
        ("procedure", "planned", "positive"),  # never overrides a planned/advised statement
        ("procedure", "current", "negative"),  # a denial has no event time
    ],
)
def test_t1_never_touches_what_it_should_not(category, temporal, assertion):
    n = normalise(category, temporal, assertion, "x", val(event_date="2019-01-01"), DOC)
    assert (n.category, n.temporal_status, n.rules) == (category, temporal, [])


def test_no_document_date_means_no_temporal_change():
    assert not normalise("procedure", "current", "positive", "colonoscopy", val(event_date="2019"), None).changed


def test_audit_block_keeps_the_originals_and_a_new_version_rederives_from_them():
    value = val(concept="HbA1c", numeric_value=53, unit="mmol/mol", event_date="2025-01-01")
    n = normalise("diagnostic_finding", "current", "positive", "hba1c", value, DOC)
    assert (n.category, n.temporal_status) == ("observation", "historical") and len(n.rules) == 2
    stored = apply_to_value(value, n, "diagnostic_finding", "current")
    assert stored["normaliser"] == {
        "version": NORMALISER_VERSION,
        "rules": n.rules,
        "original": {"category": "diagnostic_finding", "temporal_status": "current"},
    }
    assert value == val(concept="HbA1c", numeric_value=53, unit="mmol/mol", event_date="2025-01-01")  # input untouched
    assert originals("observation", "historical", stored) == ("diagnostic_finding", "current")
    assert originals("condition", "current", val()) == ("condition", "current")
    clean = normalise("condition", "current", "positive", "x", val(), DOC)
    assert apply_to_value(val(), clean, "condition", "current") == val()  # nothing changed: no audit block


def test_parse_event_date_rejects_impossible_values():
    assert parse_event_date("2026-13-01") is None and parse_event_date("2026-02-40") is None
    assert parse_event_date("5 Foo 2024") is None and parse_event_date("  2024-05  ") == (2024, 5, None)


# ---------------------------------------------------------------------------------------------------- T2 history
NOTE = "Date: 2026-03-14\nHistory\n- Gingivitis\n- Viral sinusitis\n\nMedications\n- aspirin\n\nFamily history\nMother had breast cancer.\n"


def test_section_heading_finds_the_heading_above_a_list_item_or_a_sentence():
    from anticipa.evidence.normalise import section_heading

    assert section_heading(NOTE, NOTE.index("Viral")) == "History"
    assert section_heading(NOTE, NOTE.index("aspirin")) == "Medications"
    assert section_heading(NOTE, NOTE.index("Mother")) == "Family history"
    assert section_heading(NOTE, 5) is None and section_heading(NOTE, None) is None and section_heading(NOTE, -1) is None


@pytest.mark.parametrize("heading", ["History", "Past medical history", "Medical history", "इतिहास", "पिछला इतिहास", "التاريخ المرضي"])
def test_t2_a_condition_under_a_history_heading_is_historical(heading):
    n = normalise("condition", "current", "positive", "gingivitis", val(), DOC, heading=heading)
    assert (n.temporal_status, n.rules) == ("historical", ["T2:history_section"])
    assert normalise("condition", "unknown", "positive", "x", val(), DOC, heading=heading).temporal_status == "historical"


@pytest.mark.parametrize("heading", ["Family history", "पारिवारिक इतिहास", "التاريخ العائلي", "History of smoking", "Medications", "Problem list", None])
def test_t2_only_whole_history_headings_count(heading):
    assert not normalise("condition", "current", "positive", "x", val(), DOC, heading=heading).changed


def test_t2_never_applies_to_other_subjects_categories_or_denials():
    h = "History"
    assert not normalise("condition", "current", "positive", "x", val(), DOC, subject_type="family_member", heading=h).changed
    assert not normalise("medication", "current", "positive", "x", val(), DOC, heading=h).changed
    assert not normalise("preventive_care", "unknown", "positive", "x", val(), DOC, heading=h).changed
    assert not normalise("condition", "current", "negative", "x", val(), DOC, heading=h).changed
    assert not normalise("condition", "planned", "positive", "x", val(), DOC, heading=h).changed
    assert not normalise("condition", "historical", "positive", "x", val(), DOC, heading=h).changed
    assert NORMALISER_VERSION == "n2"
