"""T-05 (matching) and T-07 (statistics) of Docs/09 §16.1 against hand-computed values."""

from __future__ import annotations

import random

import builders as b

from anticipa.evaluation.matching import concept_match, match_document, overlap, tokens
from anticipa.evaluation.stats import cluster_bootstrap_f1, latency_summary, percentile, rate, wilson


def test_wilson_matches_reference_values():
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and abs(hi - 0.2775) < 1e-3
    lo, hi = wilson(50, 100)
    assert abs(lo - 0.4038) < 1e-3 and abs(hi - 0.5962) < 1e-3
    assert wilson(10, 10)[1] > 0.999999 and abs(wilson(10, 10)[0] - 0.7225) < 1e-3
    assert wilson(0, 0) is None


def test_nearest_rank_percentile():
    xs = list(range(1, 11))
    assert percentile(xs, 0.5) == 5 and percentile(xs, 0.95) == 10 and percentile(xs, 0.1) == 1
    assert percentile([], 0.5) is None
    assert percentile([3, 1, 2], 0.5) == 2  # sorts internally


def test_latency_summary_withholds_p95_below_20_samples():
    assert latency_summary([1.0] * 19)["p95_ms"] is None
    assert latency_summary([float(i) for i in range(1, 21)])["p95_ms"] == 19.0


def test_rate_zero_denominator_is_not_applicable_never_zero_or_one():
    r = rate(0, 0)
    assert r["value"] is None and r["status"] == "not_applicable" and r["ci95"] is None
    r = rate(5, 20)
    assert r["value"] == 0.25 and r["numerator"] == 5 and r["denominator"] == 20 and "low_n" in r["flags"]
    assert "low_n" not in rate(30, 60)["flags"]


def test_cluster_bootstrap_is_seeded_and_brackets_the_point_estimate():
    clusters = [(1, 1, 1)] * 50 + [(0, 1, 1)] * 50  # F1 = 2*50 / (100+100) = 0.5
    a = cluster_bootstrap_f1(clusters, seed=7, resamples=300)
    assert a == cluster_bootstrap_f1(clusters, seed=7, resamples=300)
    assert a[0] <= 0.5 <= a[1]
    assert cluster_bootstrap_f1([(1, 1, 1)], seed=1) is None


def test_overlap_coefficient():
    assert overlap((0, 10), (5, 15)) == 0.5
    assert overlap((0, 10), (10, 20)) == 0.0
    assert overlap((0, 100), (20, 30)) == 1.0  # contained: divided by the smaller span
    assert overlap((5, 5), (0, 10)) == 0.0


def test_tokens_handle_synonyms_underscores_and_non_ascii_digits():
    assert tokens("colon_cancer") == tokens("Colorectal Cancer")
    assert tokens("breast cancer") != tokens("colon cancer")
    assert "2025" in tokens("١٩ يناير ٢٠٢٥") or "2025" in tokens("२०२५")  # Arabic-Indic or Devanagari digits -> ASCII


def test_concept_match_cases():
    lab = b.label(1, fact_type="colorectal_cancer")
    assert concept_match(b.pred_of(lab, fact_type="colon_cancer"), lab)
    assert concept_match(b.pred_of(lab, fact_type="x", concept="Colorectal cancer"), lab)
    assert not concept_match(b.pred_of(lab, fact_type="breast_cancer"), lab)
    unspecified = b.label(2, fact_type="cancer", concept_id="cancer_unspecified")
    assert concept_match(b.pred_of(unspecified, fact_type="colorectal_cancer"), unspecified)


def test_medication_ingredient_match_but_not_dosage_form_words():
    lab = b.label(1, category="medication", subject="resident", relationship=None, fact_type="acetaminophen_325_mg_oral_tablet")
    assert concept_match(b.pred_of(lab, fact_type="x", concept="Acetaminophen"), lab)
    assert concept_match(b.pred_of(lab, fact_type="acetaminophen"), lab)
    assert not concept_match(b.pred_of(lab, fact_type="x", concept="Tablet"), lab)  # a dosage form word is not an ingredient
    assert not concept_match(b.pred_of(lab, fact_type="x", concept="Ibuprofen"), lab)
    cond = b.label(2, category="condition", subject="resident", relationship=None, fact_type="chronic_periodontitis")
    assert not concept_match(b.pred_of(cond, fact_type="x", concept="Chronic"), cond)  # the ingredient rule is medication only


def test_match_document_one_to_one_duplicates_and_false_positives():
    l1, l2 = b.label(1), b.label(2, fact_type="breast_cancer")
    p1 = b.pred_of(l1)
    dup = b.pred_of(l1, salt="dup", span=(110, 130))  # same fact, narrower span: a duplicate, not a false positive
    p2 = b.pred_of(l2)
    fp = b.stray(1)
    m = match_document([l1, l2], [p1, dup, p2, fp])
    assert len(m.pairs) == 2 and len(m.duplicates) == 1 and m.false_positives == [fp] and not m.false_negatives


def test_match_prefers_concept_match_on_equal_overlap():
    lab = b.label(1)
    wrong = b.pred_of(lab, salt="a", fact_type="breast_cancer")
    right = b.pred_of(lab, salt="b")
    m = match_document([lab], [wrong, right])
    assert m.pairs[0].pred is right and m.pairs[0].concept_ok


def test_unverified_or_wrong_category_never_match_and_unmatched_label_is_a_miss():
    lab = b.label(1)
    nospan = b.pred_of(lab, span=None, quote_class="not_found")
    wrongcat = b.pred_of(lab, salt="c", category="medication")
    m = match_document([lab], [nospan, wrongcat])
    assert not m.pairs and len(m.false_positives) == 2 and m.false_negatives == [lab]


def test_matching_is_deterministic_under_reordering():
    labs = [b.label(i) for i in range(1, 8)]
    preds = [b.pred_of(label) for label in labs] + [b.pred_of(labs[0], salt="d", span=(110, 130)), b.stray(1)]
    base = {(p.label.label_id, p.pred.fact_id) for p in match_document(labs, preds).pairs}
    for seed in range(5):
        shuffled = preds[:]
        random.Random(seed).shuffle(shuffled)
        assert {(p.label.label_id, p.pred.fact_id) for p in match_document(labs, shuffled).pairs} == base


def test_hindi_and_arabic_offsets_are_code_points():
    """Spans are code-point offsets of the original text; a quote in Devanagari/Arabic is compared on those, unchanged."""
    text = "रोगी का इतिहास: भाई को कोलन कैंसर था। المريض: الأخ لديه سرطان القولون"
    start = text.index("भाई")
    lab = b.label(1, span=(start, start + 20), lang="hi")
    assert match_document([lab], [b.pred_of(lab, span=(start + 2, start + 18))]).pairs
    start_ar = text.index("الأخ")
    lab_ar = b.label(2, span=(start_ar, start_ar + 15), lang="ar")
    assert match_document([lab_ar], [b.pred_of(lab_ar)]).pairs
