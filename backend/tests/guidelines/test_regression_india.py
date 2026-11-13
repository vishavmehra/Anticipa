"""Regression suite for the India screening pack (config/guidelines/rules_india_draft.json).

Every test case of the file must produce its expected outcome (and reason, when one is stated) on THIS engine.
Expected outcomes are read from the file, never edited to make a case pass. Also validates every rule against
the rule schema, checks the pack stays a draft, that source metadata cites the Indian documents, that display
text has no dashes, and that every care activity maps to a clinic service the planner can allocate (see also
tests/caregaps/test_planning_keys.py)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from anticipa.guidelines.evaluator import Fact, record_to_fact

_HARNESS = Path(__file__).with_name("draft_harness.py")
INDIA_PATH = Path(__file__).resolve().parents[3] / "config" / "guidelines" / "rules_india_draft.json"


def _harness():
    name = "anticipa_draft_harness"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, _HARNESS)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


H = _harness()
PACK = json.loads(INDIA_PATH.read_text(encoding="utf-8"))
LIBRARY = PACK["selector_library"]
ON = date.fromisoformat(PACK["evaluation_date"])
GUIDELINES = {g["guideline_key"]: g for g in PACK["guidelines"]}
VOCABULARY = set(PACK["outcome_vocabulary"])
CASES = [(g["guideline_key"], t["id"]) for g in PACK["guidelines"] for t in g["test_cases"]]
KEYS = (
    "india_hypertension_screening",
    "india_diabetes_screening",
    "india_oral_cancer_screening",
    "india_breast_cancer_screening_cbe",
    "india_cervical_cancer_screening_via",
)
# display text: names, descriptions, labels, scopes, explanation templates (document titles are citations)
_DASH = re.compile(r"[–—]|\s-\s")


def test_pack_is_a_draft_with_the_five_guidelines_and_every_rule_validates():
    assert PACK["status"] == "draft"
    assert tuple(GUIDELINES) == KEYS
    for key, g in GUIDELINES.items():
        assert g["status"] == "draft" and g["version"]["approval_status"] == "draft", key
        assert all(t["expected"]["outcome"] in VOCABULARY for t in g["test_cases"]), key
        definition = H.definition_of(g, LIBRARY)  # raises RuleValidationError listing every problem
        assert {r.rule_id for r in definition.rules} == {r["rule_id"] for r in g["rules"]}
        assert {r["rule_id"].split(".", 1)[0] for r in g["rules"]} == {key}


def test_each_guideline_has_at_least_eight_cases_and_covers_boundaries_exclusions_and_missing_data():
    for key, g in GUIDELINES.items():
        outcomes = Counter(t["expected"]["outcome"] for t in g["test_cases"])
        assert len(g["test_cases"]) >= 8, key
        assert {"eligible", "not_eligible", "already_completed", "insufficient_evidence"} <= set(outcomes), (
            key,
            outcomes,
        )
        assert len({t["id"] for t in g["test_cases"]}) == len(g["test_cases"]), key


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


def test_rule_metadata_cites_the_indian_sources():
    cited = {
        "mohfw_og_2016",
        "mohfw_framework_cancers_2016",
        "nhm_pbs_staff_nurse_module",
        "nhm_mpw_module",
        "npncd_og_2023",
    }
    for key, g in GUIDELINES.items():
        assert g["source_organization"] == "Ministry of Health and Family Welfare, Government of India"
        ids = {s["source_id"] for s in g["sources"]}
        assert ids <= cited and g["source_reference"] in ids, key
        for s in g["sources"]:
            assert s["url"].startswith("https://") and s["retrieved_on"] == "2026-11-13" and s["excerpts"], s[
                "source_id"
            ]
        excerpt_ids = {f"{s['source_id']}#{e['id']}" for s in g["sources"] for e in s["excerpts"]}
        for r in g["rules"]:
            assert set(r["source_refs"]) <= excerpt_ids, (r["rule_id"], set(r["source_refs"]) - excerpt_ids)
            assert r["source_status"] in {
                "in_cited_source",
                "engineering_inference",
                "engineering_assumption",
                "not_in_cited_source",
            }
            if r["source_status"] == "in_cited_source":
                assert r["source_refs"], r["rule_id"]
        assert g["metadata_defaults"]["publication_dates"], key


def test_display_text_has_no_dashes():
    def texts(g):
        yield from (
            g["name"],
            g["description"],
            g["label"],
            g["population_scope"],
            g["jurisdiction"],
            g["eligible_explanation"],
        )
        for r in g["rules"]:
            yield from (
                r["name"],
                r["description"],
                r["label"],
                *(r.get("explanation_templates") or {}).values(),
            )

    for key, g in GUIDELINES.items():
        for text in texts(g):
            assert text and not _DASH.search(text), (key, text)


def test_icd10_oral_cancer_condition_matches_a_loaded_record():
    """ICD-10 selector codes use the FHIR system URI, which is how clinical_records stores them (no alias)."""
    fact = record_to_fact(
        "r1", "condition", "http://hl7.org/fhir/sid/icd-10", "C02.9", date(2024, 1, 1), None, "active", ON
    )
    assert isinstance(fact, Fact)
    selector = H.definition_of(GUIDELINES["india_oral_cancer_screening"], LIBRARY).selectors[
        "known_oral_cancer"
    ]
    assert (fact.code_system, fact.code) in selector.codeset


def test_every_care_activity_maps_to_a_planner_service():
    from anticipa.planning.adapters import CARE_ACTIVITY_SERVICE_KEYS
    from anticipa.planning.routes import KEY
    from anticipa.planning.synthetic import SERVICE_CATALOGUE

    mapped = {k: CARE_ACTIVITY_SERVICE_KEYS.get(k, k) for k in KEYS}
    assert mapped == {
        "india_hypertension_screening": "blood_pressure_check",
        "india_diabetes_screening": "blood_glucose_check",
        "india_oral_cancer_screening": "oral_cancer_screening",
        "india_breast_cancer_screening_cbe": "clinical_breast_exam",
        "india_cervical_cancer_screening_via": "cervical_screening",
    }
    for activity, service_key in mapped.items():
        assert re.match(KEY, service_key) and service_key in SERVICE_CATALOGUE, (activity, service_key)
