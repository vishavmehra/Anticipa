"""T-01 oracle, T-02 null, T-03 perturbation of Docs/09 §16.1: every metric equals its exactly computed value."""

from __future__ import annotations

import builders as b
import pytest

from anticipa.evaluation.extraction import date_equal, evaluate_extraction, parse_event_date


def build(n: int = 100):
    """n labels over 20 documents of one language: 60% relatives (mixed assertions) and 40% resident facts."""
    labels = []
    for i in range(1, n + 1):
        rel = i % 5 < 3
        labels.append(b.label(i, doc=f"d{i % 20}", subject="family_member" if rel else "resident", relationship="mother" if rel else None,
                              category="family_history" if rel else "condition", fact_type="colorectal_cancer" if rel else "diabetes",
                              assertion="negative" if i % 10 == 0 else "positive", temporal="historical" if i % 2 else "current",
                              span=(i * 100, i * 100 + 40)))  # fmt: skip
    return labels


def run(labels, preds, **kw):
    ids = sorted({lab.document_id for lab in labels} | {p.document_id for p in preds})
    return evaluate_extraction(labels, preds, b.docs(*ids), **kw)


def val(res, key, view=None):
    return res["metrics"][key]["value"]


def test_oracle_gives_perfect_scores_and_zero_critical_errors():
    labels = build()
    res = run(labels, [b.pred_of(lab) for lab in labels])
    M = res["metrics"]
    for k in ("EX-01.A", "EX-02.A", "EX-03.A", "EX-01.T", "EX-02.C", "EX-04.A.f1", "EX-05.A", "EX-08a", "EX-09", "EX-10", "EX-06"):
        assert M[k]["value"] == 1.0, k
    assert M["EX-01.A"]["numerator"] == 100 and M["EX-01.A"]["denominator"] == 100
    for k in ("SAF-01", "SAF-02", "SAF-03", "SAF-04", "SAF-05", "SAF-06", "SAF-07", "EX-12", "EX-07", "EX-18a"):
        assert (M[k].get("count", M[k]["value"]) or 0) == 0, k
    assert M["EX-12"]["denominator"] == 100 and res["scope"]["documents_evaluated"] == 20


def test_null_predictions_have_zero_recall_and_undefined_precision_without_errors():
    res = run(build(), [])
    M = res["metrics"]
    assert M["EX-02.A"]["value"] == 0.0 and M["EX-02.A"]["denominator"] == 100
    assert M["EX-01.A"]["value"] is None and M["EX-01.A"]["status"] == "not_applicable"
    assert M["EX-03.A"]["value"] is None
    assert M["EX-06"]["status"] == "not_applicable" and M["EX-12"]["status"] == "not_applicable"
    assert M["EX-08a"]["status"] == "not_applicable"


def test_perturbation_subject_assertion_temporal_drop_and_hallucination():
    labels = build()
    preds = []
    for i, lab in enumerate(labels, 1):
        kw = {}
        if i <= 10:
            kw["subject_type"] = "resident" if lab.subject_type == "family_member" else "family_member"  # flip subject on 10
        if 11 <= i <= 15:
            kw["assertion"] = "uncertain"  # flip assertion on 5
        if 16 <= i <= 19:
            kw["temporal"] = "unknown"  # shift temporal on 4
        if i == 100:
            continue  # drop 1
        preds.append(b.pred_of(lab, **kw))
    preds += [b.stray(k, doc="d1") for k in range(5)]  # 5 accepted hallucinations
    M = run(labels, preds)["metrics"]
    # 99 matched at L1/L3 in view A; 10 wrong subject + 5 wrong assertion + 4 wrong temporal are not strict
    assert M["EX-05.A"]["numerator"] == 99 and M["EX-05.A"]["denominator"] == 100
    assert M["EX-04.A.recall"]["numerator"] == 99
    assert M["EX-08a"]["numerator"] == 89 and M["EX-08a"]["denominator"] == 99
    assert M["EX-09"]["numerator"] == 94 and M["EX-10"]["numerator"] == 95
    assert M["EX-02.A"]["numerator"] == 99 - 10 - 5 - 4  # strict needs all of subject/assertion/temporal
    assert M["EX-01.A"]["denominator"] == 104 and M["EX-12"]["numerator"] == 5 and M["EX-12"]["denominator"] == 104
    assert M["EX-12"]["value"] == pytest.approx(5 / 104, abs=1e-6)


def test_negation_flip_wrong_subject_planned_historical_uncertain_are_counted_separately():
    neg = [b.label(i, assertion="negative") for i in range(1, 21)]  # 20 negated relatives
    rel = [b.label(i, subject="family_member", relationship="sister") for i in range(21, 41)]  # 20 relatives
    planned = [b.label(i, subject="resident", relationship=None, category="procedure", fact_type="colonoscopy", temporal="planned") for i in range(41, 51)]
    hist = [b.label(i, subject="resident", relationship=None, category="condition", fact_type="diabetes", temporal="historical") for i in range(51, 61)]
    unc = [b.label(i, subject="resident", relationship=None, category="condition", fact_type="asthma", assertion="uncertain") for i in range(61, 71)]
    labels = neg + rel + planned + hist + unc
    preds = (
        [b.pred_of(lab, assertion="positive") if k < 3 else b.pred_of(lab) for k, lab in enumerate(neg)]  # 3 negation flips
        + [b.pred_of(lab, subject_type="resident", relationship=None) if k < 4 else b.pred_of(lab) for k, lab in enumerate(rel)]  # 4 misattributed
        + [b.pred_of(lab, temporal="historical") if k < 2 else b.pred_of(lab) for k, lab in enumerate(planned)]  # 2 planned-as-completed
        + [b.pred_of(lab, temporal="current") if k < 1 else b.pred_of(lab) for k, lab in enumerate(hist)]  # 1 historical-as-current
        + [b.pred_of(lab, assertion="positive") if k < 5 else b.pred_of(lab) for k, lab in enumerate(unc)]  # 5 uncertain-as-certain
    )
    res = run(labels, preds)
    M = res["metrics"]
    # SAF-01 denominator: all non-resident labels = neg(20) + rel(20); numerator the 4 misattributed relatives
    assert (M["SAF-01"]["count"], M["SAF-01"]["denominator"]) == (4, 40)
    assert (M["SAF-02"]["count"], M["SAF-02"]["denominator"]) == (3, 20)
    assert (M["SAF-03"]["count"], M["SAF-03"]["denominator"]) == (2, 10)
    assert (M["SAF-04"]["count"], M["SAF-04"]["denominator"]) == (1, 60)  # historical labels: neg 20 + rel 20 + hist 10 + uncertain 10 (all default to historical)
    assert (M["SAF-07"]["count"], M["SAF-07"]["denominator"]) == (5, 10)
    assert M["SAF-02"]["value"] == 0.15
    cases = res["safety_cases"]
    assert cases["SAF-02"]["count"] == 3 and all(c["label_id"] and c["fact_id"] for c in cases["SAF-02"]["cases"])
    assert "quote" not in str(cases)  # ids only: never text


def test_missing_documentation_read_as_negative_and_missing_evidence_false_positive():
    present = b.label(1)
    absent_a = b.label(2, present=False, subject="family_member", fact_type="ovarian_cancer", concept_id="ovarian_cancer", assertion="positive")
    absent_b = b.label(3, present=False, subject="family_member", fact_type="breast_cancer", concept_id="breast_cancer")
    absent_c = b.label(4, present=False, subject="family_member", fact_type="prostate_cancer", concept_id="prostate_cancer")
    preds = [b.pred_of(present),
             b.stray(1, category="family_history", fact_type="ovarian_cancer", subject_type="family_member", relationship="mother", assertion="negative"),  # SAF-06
             b.stray(2, category="family_history", fact_type="breast_cancer", subject_type="family_member", relationship="mother", assertion="positive")]  # EX-14 only
    M = run([present, absent_a, absent_b, absent_c], preds)["metrics"]
    assert (M["EX-14"]["numerator"], M["EX-14"]["denominator"]) == (2, 3)
    assert (M["SAF-06"]["count"], M["SAF-06"]["denominator"]) == (1, 3)


def test_prompt_injection_compliance_is_measured_per_document():
    lab_clean = b.label(1, doc="d1")
    inj1 = b.label(2, doc="d1", present=False, category="preventive_care", fact_type="screening_eligibility_claim", concept_id=None, subject="resident", injected=(5000, 5090))
    inj2 = b.label(3, doc="d2", present=False, category="preventive_care", fact_type="screening_eligibility_claim", concept_id=None, subject="resident", injected=(6000, 6090))
    complied = b.stray(1, doc="d1", span=(5010, 5050))  # fact derived from the injected span
    M = run([lab_clean, inj1, inj2], [b.pred_of(lab_clean), complied])["metrics"]
    assert (M["SAF-08"]["count"], M["SAF-08"]["denominator"]) == (1, 2)


def test_accepted_fact_without_verified_quote_is_flagged_saf05():
    lab = b.label(1)
    bad = b.pred_of(lab, salt="x", quote_class="not_found", span=None)
    M = run([lab], [b.pred_of(lab), bad])["metrics"]
    assert M["SAF-05"]["count"] == 1 and M["SAF-05"]["denominator"] == 2
    assert M["EX-06"]["numerator"] == 1 and M["EX-06"]["denominator"] == 2 and M["EX-07"]["numerator"] == 1
    assert M["EX-12c"]["numerator"] == 1  # unsupported claim: no label and unverified quote


def test_views_a_t_c_differ_by_disposition():
    labs = [b.label(i, fact_type="diabetes", category="condition", subject="resident", relationship=None) for i in range(1, 11)]
    preds = [b.pred_of(lab, disposition="accepted" if i <= 6 else "review" if i <= 8 else "rejected") for i, lab in enumerate(labs, 1)]
    M = run(labs, preds)["metrics"]
    assert M["EX-02.A"]["numerator"] == 6 and M["EX-02.T"]["numerator"] == 8 and M["EX-02.C"]["numerator"] == 10
    assert M["EX-01.A"]["value"] == 1.0 and M["EX-18a"]["numerator"] == 2  # two facts routed to review


def test_language_breakdown_isolates_the_perturbed_language():
    en = [b.label(i, doc="den", res="r-en", lang="en") for i in range(1, 21)]
    hi = [b.label(i, doc="dhi", res="r-hi", lang="hi") for i in range(21, 41)]
    preds = [b.pred_of(lab) for lab in en] + [b.pred_of(lab, assertion="uncertain") if k < 5 else b.pred_of(lab) for k, lab in enumerate(hi)]
    docs = {**b.docs("den", res="r-en"), **b.docs("dhi", lang="hi", res="r-hi")}
    M = evaluate_extraction(en + hi, preds, docs)["metrics"]
    by = M["EX-09"]["breakdowns"]["language"]
    assert by["en"]["value"] == 1.0 and by["hi"]["value"] == 0.75 and by["hi"]["numerator"] == 15 and by["hi"]["denominator"] == 20


def test_failed_documents_are_misses_and_unprocessed_documents_are_not():
    lab_failed, lab_pending = b.label(1, doc="failed"), b.label(2, doc="pending")
    docs = b.docs("failed", status="extraction_failed")
    res = evaluate_extraction([lab_failed, lab_pending], [], docs)
    assert res["metrics"]["EX-02.A"]["denominator"] == 1  # the failed document's label is a miss
    assert res["scope"]["labels_in_unprocessed_documents"] == 1 and res["scope"]["documents_failed"] == 1


def test_event_date_age_and_forbidden_interpretation_metrics():
    d = {"value": "2024-10-10", "precision": "day"}
    labs = [b.label(1, event_date=d, age=52), b.label(2, event_date={"value": "2023", "precision": "year"}, age=40),
            b.label(3, forbidden=({"subject": "resident"},))]  # fmt: skip
    preds = [b.pred_of(labs[0], event_date="10 October 2024", age_at_onset=52), b.pred_of(labs[1], event_date="2022", age_at_onset=41),
             b.pred_of(labs[2], subject_type="resident", relationship=None)]  # fmt: skip
    M = run(labs, preds)["metrics"]
    assert (M["EX-11a"]["numerator"], M["EX-11a"]["denominator"]) == (1, 2) and (M["EX-11b"]["numerator"], M["EX-11b"]["denominator"]) == (1, 2)
    assert M["SAF-09"]["count"] == 1


def test_conflict_preservation_counts_members_that_survive_as_separate_facts():
    l1, l2 = b.label(1, group="g"), b.label(2, group="g", assertion="negative")
    groups = [{"member_label_ids": [l1.label_id, l2.label_id]}]
    full = run([l1, l2], [b.pred_of(l1), b.pred_of(l2)], conflict_groups=groups)["metrics"]["EX-15c"]
    lost = run([l1, l2], [b.pred_of(l1)], conflict_groups=groups)["metrics"]["EX-15c"]
    assert full["value"] == 1.0 and lost["value"] == 0.5 and lost["groups"] == 1


def test_parse_event_date_formats_and_precision_comparison():
    assert parse_event_date("2025-01-19") == (2025, 1, 19)
    assert parse_event_date("19 January 2025") == (2025, 1, 19)
    assert parse_event_date("January 19, 2025") == (2025, 1, 19)
    assert parse_event_date("Jan 2025") == (2025, 1, None)
    assert parse_event_date("1 सितंबर 2023") == (2023, 9, 1)
    assert parse_event_date("١٩ يناير ٢٠٢٥") == (2025, 1, 19)
    assert parse_event_date("sometime last year") is None and parse_event_date(None) is None
    assert date_equal("2025-01-19", {"value": "2025", "precision": "year"}) is True
    assert date_equal("2025-02-19", {"value": "2025-01", "precision": "month"}) is False
    assert date_equal("garbled", {"value": "2025", "precision": "year"}) is None


def test_confusion_matrix_and_certainty_table_are_reported():
    labs = [b.label(i, assertion="negative" if i <= 4 else "positive") for i in range(1, 9)]
    preds = [b.pred_of(lab, assertion="positive") if lab.assertion == "negative" and i <= 2 else b.pred_of(lab) for i, lab in enumerate(labs, 1)]
    M = run(labs, preds)["metrics"]
    assert M["EX-09"]["confusion"]["negative"] == {"negative": 2, "positive": 2}
    assert M["EX-19"]["high"]["denominator"] == 8


def test_taxonomy_mismatch_is_separated_from_a_real_miss():
    found_elsewhere, missed = b.label(1, category="observation", subject="resident", relationship=None, fact_type="blood_pressure"), b.label(2, category="observation", subject="resident", relationship=None, fact_type="bmi")
    preds = [b.pred_of(found_elsewhere, category="diagnostic_finding")]  # same span, a category the ground truth does not label
    res = run([found_elsewhere, missed], preds)
    M = res["metrics"]
    assert M["EX-05.A"]["numerator"] == 0 and (M["EX-05c"]["numerator"], M["EX-05c"]["denominator"]) == (1, 2)
    assert res["scope"]["facts_in_unscored_categories"] == {"diagnostic_finding": 1}
