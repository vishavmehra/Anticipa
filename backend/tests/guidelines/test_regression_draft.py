"""Regression suite: every test case shipped in config/guidelines/rules_draft.json must produce its expected
outcome
(and reason, when one is stated) on THIS engine. Expected outcomes are read from the file, never edited to
a case pass; a failing case is a finding to report. Also validates every rule of the file against the rule
schema."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).with_name("draft_harness.py")


def _harness():
    name = "anticipa_draft_harness"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, _PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


H = _harness()
DRAFT = H.load_draft()
LIBRARY = DRAFT["selector_library"]
ON = H.evaluation_date(DRAFT)
GUIDELINES = {g["guideline_key"]: g for g in DRAFT["guidelines"]}
OUTCOME_VOCABULARY = set(DRAFT["outcome_vocabulary"])


def expected_vocabulary_ok(g) -> bool:
    return all(t["expected"]["outcome"] in OUTCOME_VOCABULARY for t in g["test_cases"])


CASES = H.all_cases()


def test_draft_is_a_draft_and_every_guideline_validates_against_the_schema():
    assert DRAFT["status"] == "draft"
    for key, g in GUIDELINES.items():
        assert g["status"] == "draft" and g["version"]["approval_status"] == "draft", key
        assert expected_vocabulary_ok(g)
        definition = H.definition_of(g, LIBRARY)  # raises RuleValidationError listing every problem
        assert {r.rule_id for r in definition.rules} == {r["rule_id"] for r in g["rules"]}


def test_case_count_is_not_silently_shrinking():
    assert len(CASES) >= 181


@pytest.mark.parametrize("guideline_key,case_id", CASES, ids=[f"{g}:{c}" for g, c in CASES])
def test_expected_outcome(guideline_key, case_id):
    g = GUIDELINES[guideline_key]
    case = next(t for t in g["test_cases"] if t["id"] == case_id)
    result = H.run_case(g, case, LIBRARY, ON)
    expected = case["expected"]
    detail = (
        f"{case['description']} -> got {result.outcome_class}/{result.outcome}/{result.reason_code}; "
        + "; ".join(
            f"{c.rule_id.split('.', 1)[-1]}={c.outcome}" for c in result.criteria if c.outcome != "passed"
        )
    )
    assert result.outcome == expected["outcome"], detail
    if "reason" in expected:
        assert result.reason_code == expected["reason"], detail
