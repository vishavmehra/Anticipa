# ruff: noqa: E501
"""Deterministic numeric grounding: what an answer may quote."""

from __future__ import annotations

from anticipa.ask.grounding import allowed_values, numbers_in, ungrounded

RES = [
    {
        "values": {"cases": 116411, "closure_rate_overall": 0.0065},
        "rows": [
            {"label": "Indore (D02)", "closure_rate": 0.0412, "cases": 12000},
            {"label": "Pune (D05)", "closure_rate": 0.0089, "cases": 9000},
        ],
        "definition": "closure over the last 30 days",
    }
]


def check(text, *context):
    return ungrounded(text, allowed_values(RES, *context))


def test_values_percentages_rounding_thousands_and_gaps_are_grounded():
    assert check("Indore (D02) closed 4.1% of 12,000 cases and Pune 0.9%.") == []
    assert check("The gap is 3.2 points.") == []  # 4.12 - 0.89, in percentage points
    assert check("There are 116,411 cases, about 116 thousand.") == []
    assert check("Overall closure is 0.65%.") == []


def test_invented_or_changed_numbers_are_caught():
    assert check("Indore closed 5.1% of cases.") == ["5.1"]
    assert check("There are 120,000 cases.") == ["120,000"]
    assert check("Pune has 9,001 cases.") == ["9,001"]


def test_numbers_from_the_question_dates_years_and_codes_are_not_flagged():
    assert check("Over the last 30 days, 2026 data for D07 and 13 Nov on 2026-11-13.", "show the last 30 days") == []
    assert check("Fewer than 10 cases", "") == ["10"]  # a bare number nobody supplied is flagged


def test_scale_words_multiply():
    assert numbers_in("1.2 million residents")[0][1] == 1_200_000
    assert ungrounded("1.2 million", {1_200_000.0}) == []
    assert ungrounded("1.2 million", {2_000_000.0}) == ["1.2"]


def test_a_ratio_and_its_percentage_are_both_allowed_but_not_others():
    assert check("closure 4.12%") == []
    assert check("closure 0.0412") == []
    assert check("closure 41.2%") == ["41.2"]
