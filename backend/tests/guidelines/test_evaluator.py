"""Deterministic evaluator (08 §7-§10, §16; rules schema v2): the seven outcomes, decision-policy ranking,
missing-data
semantics, subject separation, unresolved evidence, conflicts, interval aggregates, temporal rules,
determinism, source
references and the incremental-evaluation input hash. Unit tests, all data synthetic."""

from __future__ import annotations

import ast
import json
import random
import uuid
from datetime import date
from pathlib import Path

import pytest

import anticipa.guidelines.evaluator as evaluator_module
from anticipa.guidelines.evaluator import (
    Fact,
    GuidelineDeps,
    ResidentFacts,
    ResidentInputs,
    age_years,
    evaluate_guideline,
    input_hash,
    parse_partial_date,
    record_to_fact,
    shift_back,
)
from anticipa.guidelines.rules import OUTCOME_CLASS, OUTCOMES, parse_guideline_definition

ON = date(2026, 11, 8)
FIRST_DEGREE = ["mother", "father", "sister", "brother", "daughter", "son"]
SEL = {
    "cov": {
        "category_in": ["any"],
        "fact_type_in": ["any"],
        "codes": [],
        "subject_type_in": ["resident"],
        "assertion_status_in": ["positive"],
        "temporal_status_in": ["current", "historical"],
        "lookback_years": 5,
    },
    "done": {
        "category_in": ["procedure"],
        "fact_type_in": ["thing_done"],
        "codes": [{"system": "SNOMED-CT", "code": "111"}],
        "subject_type_in": ["resident"],
        "assertion_status_in": ["positive"],
        "temporal_status_in": ["current", "historical"],
    },
    "planned": {
        "category_in": ["procedure"],
        "fact_type_in": ["thing_done"],
        "codes": [],
        "subject_type_in": ["resident"],
        "assertion_status_in": ["positive"],
        "temporal_status_in": ["planned"],
    },
    "excl": {
        "category_in": ["condition"],
        "fact_type_in": ["bad_condition"],
        "codes": [],
        "subject_type_in": ["resident"],
        "assertion_status_in": ["positive"],
        "temporal_status_in": ["current", "historical"],
    },
    "fam": {
        "category_in": ["family_history"],
        "fact_type_in": ["bad_condition"],
        "codes": [],
        "subject_type_in": ["family_member"],
        "assertion_status_in": ["positive"],
        "temporal_status_in": ["current", "historical", "unknown"],
        "relationship_in": FIRST_DEGREE,
        "unresolved_relationship_in": ["cousin", "other_relative"],
    },
    "fam_b": {
        "category_in": ["family_history"],
        "fact_type_in": ["other_condition"],
        "codes": [],
        "subject_type_in": ["family_member"],
        "assertion_status_in": ["positive"],
        "temporal_status_in": ["current", "historical", "unknown"],
        "relationship_in": FIRST_DEGREE,
    },
}
PASS_F = {"true": "passed", "false": "failed", "unknown": "unknown"}
FAIL_T = {"true": "failed", "false": "passed", "unknown": "unknown"}
OBR_INCLUSION = {"true": None, "false": "not_eligible", "unknown": "insufficient_evidence"}


def obr_when_true(outcome):
    return {"true": outcome, "false": None, "unknown": "insufficient_evidence"}


def lf(field, operator, value=None):
    return {"field": field, "operator": operator, "value": value}


def exists(sel):
    return lf("evidence.selector", "EXISTS", sel)


def rule(rid, rtype, priority, expr, sem, obr, reason, **kw):
    return {
        "rule_id": f"g.{rid}",
        "name": rid,
        "rule_type": rtype,
        "priority": priority,
        "expression": expr,
        "criterion_semantics": sem,
        "outcome_by_expression_result": obr,
        "reason_code": reason,
        **kw,
    }


def incl(rid, priority, expr, reason, **kw):
    return rule(rid, "inclusion", priority, expr, PASS_F, OBR_INCLUSION, reason, **kw)


def excl(rid, priority, expr, reason, outcome="not_eligible", **kw):
    return rule(rid, "exclusion", priority, expr, FAIL_T, obr_when_true(outcome), reason, **kw)


def base_rules():
    return [
        rule(
            "cov",
            "prerequisite",
            1,
            exists("cov"),
            {"true": "passed", "false": "unknown", "unknown": "unknown"},
            {"true": None, "false": "insufficient_evidence", "unknown": "insufficient_evidence"},
            "no_coverage",
        ),
        incl(
            "age_min",
            10,
            lf("resident.age", "GREATER_THAN_OR_EQUAL", 40),
            "too_young",
            reason_code_when_unknown="age_unknown",
            source_refs=["src#E1"],
            source_metadata={"recommendation_status": {"src": "final"}, "evidence_grade": "B"},
            explanation_templates={"false": "Under the minimum age.", "unknown": "Age unknown."},
        ),
        incl("age_max", 11, lf("resident.age", "LESS_THAN_OR_EQUAL", 75), "too_old"),
        excl("excl_personal", 20, exists("excl"), "personal_history"),
        rule(
            "done_recently",
            "temporal",
            40,
            {
                "operator": "AND",
                "conditions": [
                    exists("done"),
                    lf("evidence.done.latest_event_date", "DATE_WITHIN", {"years": 5}),
                ],
            },
            FAIL_T,
            obr_when_true("already_completed"),
            "up_to_date",
            reason_code_when_unknown="completion_unknown",
        ),
        rule(
            "planned",
            "temporal",
            41,
            exists("planned"),
            FAIL_T,
            obr_when_true("temporarily_excluded"),
            "planned",
        ),
    ]


SOURCES = [
    {
        "source_id": "src",
        "title": "A synthetic source",
        "url": "https://example.invalid/s",
        "publication_date": "2026-01-01",
        "citation": "c",
        "grade_or_strength": "B",
    }
]


def defn(rules=None, policy=None):
    return parse_guideline_definition(rules or base_rules(), SEL, policy)


def person(birth=date(1980, 1, 1), sex="female", deceased=None):
    return ResidentFacts(uuid.uuid4(), birth, deceased, sex)


def fact(
    category="procedure",
    fact_type="thing_done",
    subject_type="resident",
    relationship=None,
    assertion="positive",
    temporal="historical",
    validation="accepted",
    start=None,
    end=None,
    onset=None,
    key=None,
    doc=None,
    quote=None,
    **kw,
):
    return Fact(
        str(uuid.uuid4()),
        "fact",
        category,
        fact_type,
        None,
        None,
        subject_type,
        relationship,
        key or relationship or "self",
        assertion,
        temporal,
        validation,
        start,
        end if end is not None else start,
        onset,
        doc,
        quote,
        **kw,
    )


def relative(who="mother", fact_type="bad_condition", **kw):
    return fact("family_history", fact_type, "family_member", who, **kw)


def inp(res=None, facts=(), covered=True):
    return ResidentInputs(res or person(), tuple(facts), date(2026, 6, 1) if covered else None)


def ev(inputs, rules=None, on=ON, policy=None, eligible_text=None):
    return evaluate_guideline(defn(rules, policy), inputs, on, SOURCES, eligible_text)


def one(expr, sem=PASS_F, obr=OBR_INCLUSION, **kw):
    """A guideline with a single rule, for testing one expression in isolation."""
    return [rule("r", "inclusion", 5, expr, sem, obr, "why", **kw)]


# ---------------------------------------------------------------------------------------------- dates and
# partial dates
def test_age_whole_years_and_calendar_shifts():
    assert age_years(date(1980, 11, 8), ON) == 46 and age_years(date(1980, 11, 9), ON) == 45
    assert shift_back(date(2026, 11, 8), 5) == date(2021, 11, 8)
    assert shift_back(date(2024, 2, 29), 1) == date(2023, 2, 28)
    assert shift_back(date(2026, 3, 31), months=1) == date(2026, 2, 28)


def test_partial_dates_carry_their_full_range():
    assert parse_partial_date("2018") == (date(2018, 1, 1), date(2018, 12, 31))
    assert parse_partial_date("2018-02") == (date(2018, 2, 1), date(2018, 2, 28))
    assert parse_partial_date("2018-02-03") == (date(2018, 2, 3), date(2018, 2, 3))
    assert parse_partial_date("last spring") is None and parse_partial_date(None) is None


# ---------------------------------------------------------------------------------------------- outcomes and
# ranking
def test_eligible_with_criterion_level_explanation_and_citations():
    res = ev(inp(), eligible_text="Eligible: a synthetic explanation.")
    assert (res.outcome, res.outcome_class, res.reason_code, res.basis) == (
        "eligible",
        "eligible",
        None,
        "all_criteria_met",
    )
    assert res.explanation_text == "Eligible: a synthetic explanation."
    assert {c.outcome for c in res.criteria} == {"passed"} and len(res.criteria) == 6
    expl = res.explanation({"key": "g"}, ON)
    assert expl["criteria"][0]["criterion_id"] == "g.cov" and "reason_code" not in expl["criteria"][0]
    first = res.source_references[0]  # eligible: the inclusion rules carry the citations
    assert first["rule_id"] == "g.age_min" and first["references"][0]["title"] == "A synthetic source"
    assert first["metadata"]["source_metadata"]["evidence_grade"] == "B"


@pytest.mark.parametrize(
    "build,outcome,reason",
    [
        (lambda: inp(person(birth=date(2000, 1, 1))), "not_eligible", "too_young"),
        (lambda: inp(person(birth=date(1940, 1, 1))), "not_eligible", "too_old"),
        (lambda: inp(facts=[fact("condition", "bad_condition")]), "not_eligible", "personal_history"),
        (lambda: inp(facts=[fact(start=date(2024, 1, 1))]), "already_completed", "up_to_date"),
        (lambda: inp(facts=[fact(temporal="planned")]), "temporarily_excluded", "planned"),
        (lambda: inp(covered=False), "insufficient_evidence", "no_coverage"),
        (lambda: inp(person(birth=None)), "insufficient_evidence", "age_unknown"),
        (lambda: inp(person(deceased=date(2025, 1, 1))), "not_eligible", "resident_deceased"),
        (lambda: inp(person(birth=date(2030, 1, 1))), "evaluation_error", "evaluation_error"),
    ],
)
def test_outcome_types(build, outcome, reason):
    res = ev(build())
    assert (res.outcome, res.reason_code) == (outcome, reason)
    assert res.outcome_class == OUTCOME_CLASS[outcome]


def test_requires_clinical_review_and_all_seven_outcomes_are_reachable():
    review_rule = excl(
        "review_me",
        30,
        exists("excl"),
        "needs_clinician",
        outcome="requires_clinical_review",
        review_class="out_of_scope",
    )
    policy = {
        "steps": [
            {"rank": 1, "outcome": "not_eligible"},
            {"rank": 2, "outcome": "requires_clinical_review"},
            {"rank": 3, "outcome": "already_completed"},
            {"rank": 4, "outcome": "temporarily_excluded"},
            {"rank": 5, "outcome": "evaluation_error"},
            {"rank": 6, "outcome": "insufficient_evidence"},
            {"otherwise": "eligible"},
        ]
    }
    rules = [r for r in base_rules() if r["rule_id"] != "g.excl_personal"] + [review_rule]
    review = ev(inp(facts=[fact("condition", "bad_condition")]), rules=rules, policy=policy)
    assert (review.outcome, review.outcome_class, review.reason_code) == (
        "requires_clinical_review",
        "indeterminate",
        "needs_clinician",
    )
    seen = {
        review.outcome,
        ev(inp()).outcome,
        ev(inp(person(birth=date(2000, 1, 1)))).outcome,
        ev(inp(facts=[fact(start=date(2024, 1, 1))])).outcome,
        ev(inp(covered=False)).outcome,
        ev(inp(facts=[fact(temporal="planned")])).outcome,
        ev(inp(person(birth=date(2030, 1, 1)))).outcome,
    }
    assert seen == set(OUTCOMES)


def test_decision_policy_ranks_contributions_and_review_class_restricts_a_step():
    a = excl(
        "high_risk",
        30,
        exists("excl"),
        "high_risk",
        outcome="requires_clinical_review",
        review_class="out_of_scope",
    )
    b = excl(
        "judgement",
        31,
        exists("excl"),
        "judgement",
        outcome="requires_clinical_review",
        review_class="individual_decision",
    )
    done = rule(
        "done", "temporal", 40, exists("done"), FAIL_T, obr_when_true("already_completed"), "up_to_date"
    )
    # the draft's convention: out-of-scope review outranks "already completed"; an individual-decision review
    # does not
    policy = {
        "steps": [
            {"rank": 1, "outcome": "not_eligible"},
            {
                "rank": 2,
                "outcome": "requires_clinical_review",
                "only_rules_with_review_class": "out_of_scope",
            },
            {"rank": 3, "outcome": "already_completed"},
            {
                "rank": 4,
                "outcome": "requires_clinical_review",
                "only_rules_with_review_class": "individual_decision",
            },
            {"rank": 5, "outcome": "insufficient_evidence"},
            {"otherwise": "eligible"},
        ]
    }
    cond, completed = fact("condition", "bad_condition"), fact(start=date(2024, 1, 1))
    both = ev(inp(facts=[cond, completed]), rules=[a, done], policy=policy)
    assert (both.outcome, both.reason_code) == ("requires_clinical_review", "high_risk")
    mild = ev(inp(facts=[cond, completed]), rules=[b, done], policy=policy)
    assert (mild.outcome, mild.reason_code) == ("already_completed", "up_to_date")
    assert ev(inp(facts=[cond]), rules=[b, done], policy=policy).reason_code == "judgement"


def test_a_contribution_no_step_ranks_is_rejected_at_validation():
    from anticipa.guidelines.rules import RuleValidationError

    orphan = excl(
        "orphan", 30, exists("excl"), "orphan", outcome="requires_clinical_review", review_class="mystery"
    )
    policy = {
        "steps": [
            {"rank": 1, "outcome": "not_eligible"},
            {"rank": 2, "outcome": "requires_clinical_review", "only_rules_with_review_class": "other"},
            {"otherwise": "eligible"},
        ]
    }
    with pytest.raises(RuleValidationError, match="no decision step ranks it"):
        defn([orphan], policy=policy)


def test_primary_reason_is_the_lowest_priority_number_of_the_winning_outcome():
    res = ev(
        inp(person(birth=date(2000, 1, 1)), [fact("condition", "bad_condition")])
    )  # both contribute not_eligible
    assert (res.reason_code, res.decisive_rule_id) == ("too_young", "g.age_min")
    assert [e["rule_id"] for e in res.exclusion_reasons] == ["g.excl_personal"]


def test_a_definite_contribution_outranks_insufficient_evidence_but_unknowns_block_eligibility():
    assert (
        ev(inp(person(birth=date(2000, 1, 1)), covered=False)).outcome == "not_eligible"
    )  # rank 1 beats rank 6
    only_unknown = ev(inp(covered=False))
    assert only_unknown.outcome_class == "indeterminate" and [
        m["criterion_id"] for m in only_unknown.missing_information
    ] == ["g.cov"]


def test_explanation_templates_are_used_verbatim_for_the_criterion_detail():
    res = ev(inp(person(birth=date(2000, 1, 1))))
    assert next(c for c in res.criteria if c.rule_id == "g.age_min").detail == "Under the minimum age."
    assert res.explanation_text == "Under the minimum age."
    assert (
        "Age 26" in next(c for c in res.criteria if c.rule_id == "g.age_max").detail
    )  # no template: generated from the evaluation


# ---------------------------------------------------------------------------------------------- missing data
# and logic
def test_missing_evidence_is_not_a_negative_finding():
    res = ev(inp(facts=[fact(start=None)]))  # a completion fact with no date cannot be placed in the window
    assert (res.outcome, res.reason_code) == ("insufficient_evidence", "completion_unknown")
    required = one(
        exists("fam")
    )  # the RULE's own semantics decide that absence fails the criterion, not the engine
    assert ev(inp(), rules=required).outcome == "not_eligible"
    unknown_rule = one(lf("evidence.fam.min_age_at_onset", "EQUALS", 50))
    assert (
        ev(inp(), rules=unknown_rule).outcome == "not_eligible"
    )  # no subjects at all: minimum is +infinity, which is decided
    assert (
        ev(inp(facts=[relative(onset=None)]), rules=unknown_rule).outcome == "insufficient_evidence"
    )  # unknown age: undecidable


def test_missing_data_policy_overrides():
    leaf = lf("evidence.fam.min_age_at_onset", "LESS_THAN", 60)
    unknown_input = inp(
        facts=[relative("mother", onset=None), relative("father", onset=66)]
    )  # interval: undecidable
    assert (
        ev(unknown_input, rules=one(leaf, missing_data_policy="unknown")).outcome == "insufficient_evidence"
    )
    assert (
        ev(unknown_input, rules=one(leaf, missing_data_policy="not_satisfied")).outcome == "not_eligible"
    )  # unknown treated as false
    review = [
        rule(
            "r",
            "inclusion",
            5,
            leaf,
            PASS_F,
            OBR_INCLUSION,
            "why",
            missing_data_policy="review_required",
            review_class="judgement",
        )
    ]
    policy = {
        "steps": [
            {"rank": 1, "outcome": "not_eligible"},
            {"rank": 2, "outcome": "requires_clinical_review", "only_rules_with_review_class": "judgement"},
            {"rank": 3, "outcome": "insufficient_evidence"},
            {"otherwise": "eligible"},
        ]
    }
    assert ev(unknown_input, rules=review, policy=policy).outcome == "requires_clinical_review"


def test_kleene_logic_in_expressions():
    unknown = lf("evidence.fam.min_age_at_onset", "LESS_THAN", 60)
    true_, false_ = lf("resident.age", "GREATER_THAN_OR_EQUAL", 40), lf("resident.age", "LESS_THAN", 18)
    unknown_input = inp(facts=[relative("mother", onset=None), relative("father", onset=66)])

    def outcome(expr):
        return ev(unknown_input, rules=one(expr)).outcome

    assert outcome({"operator": "AND", "conditions": [false_, unknown]}) == "not_eligible"
    assert outcome({"operator": "AND", "conditions": [true_, unknown]}) == "insufficient_evidence"
    assert outcome({"operator": "OR", "conditions": [true_, unknown]}) == "eligible"
    assert outcome({"operator": "OR", "conditions": [false_, unknown]}) == "insufficient_evidence"
    assert outcome({"operator": "NOT", "conditions": [unknown]}) == "insufficient_evidence"
    assert outcome({"operator": "NOT", "condition": false_}) == "eligible"


def test_sex_assigned_at_birth_field():
    r = one(
        {
            "operator": "AND",
            "conditions": [
                lf("resident.age", "GREATER_THAN_OR_EQUAL", 40),
                lf("resident.sex_assigned_at_birth", "EQUALS", "female"),
            ],
        }
    )
    assert ev(inp(person(sex="female")), rules=r).outcome == "eligible"
    assert ev(inp(person(sex="male")), rules=r).outcome == "not_eligible"
    assert ev(inp(person(sex="unknown")), rules=r).outcome == "insufficient_evidence"
    assert ev(inp(person(sex=None)), rules=r).outcome == "insufficient_evidence"


# ---------------------------------------------------------------------------------------------- selectors and
# evidence
def test_relatives_facts_are_never_attributed_to_the_resident_and_vice_versa():
    own_rule = [excl("excl", 20, exists("excl"), "personal")]
    assert (
        ev(inp(facts=[relative("mother", "bad_condition")]), rules=own_rule).outcome == "eligible"
    )  # mother's disease is not hers
    fam_rule = one(exists("fam"))
    assert (
        ev(inp(facts=[fact("condition", "bad_condition", "resident")]), rules=fam_rule).outcome
        == "not_eligible"
    )  # her own is not a relative's
    others = [
        fact("family_history", "bad_condition", "other"),
        fact("family_history", "bad_condition", "unknown"),
    ]
    assert ev(inp(facts=others), rules=fam_rule).outcome == "not_eligible"
    assert ev(inp(facts=[relative("mother")]), rules=fam_rule).outcome == "eligible"


def test_only_accepted_evidence_is_definite():
    fam_rule = one(exists("fam"))
    for status in ("requires_review", "pending", "schema_valid", "source_verified"):
        assert (
            ev(inp(facts=[relative(validation=status)]), rules=fam_rule).outcome == "insufficient_evidence"
        ), status  # unresolved candidate
    for status in ("rejected", "failed"):
        assert ev(inp(facts=[relative(validation=status)]), rules=fam_rule).outcome == "not_eligible", (
            status
        )  # ignored altogether


def test_uncertain_conditional_unknown_timing_and_undecidable_relatives_stay_unresolved():
    fam_rule = one(exists("fam"))
    for assertion in ("uncertain", "conditional"):
        assert (
            ev(inp(facts=[relative(assertion=assertion)]), rules=fam_rule).outcome == "insufficient_evidence"
        ), assertion
    assert (
        ev(inp(facts=[relative("cousin")]), rules=fam_rule).outcome == "insufficient_evidence"
    )  # relevance of a cousin cannot be decided
    own = [excl("excl", 20, exists("excl"), "personal")]
    assert (
        ev(inp(facts=[fact("condition", "bad_condition", temporal="unknown")]), rules=own).outcome
        == "insufficient_evidence"
    )
    done_rule = [
        rule("done", "temporal", 40, exists("done"), FAIL_T, obr_when_true("already_completed"), "done")
    ]
    assert (
        ev(inp(facts=[fact(temporal="planned")]), rules=done_rule).outcome == "eligible"
    )  # a planned procedure is not a completed one
    assert (
        ev(inp(facts=[fact(assertion="negative", start=date(2024, 1, 1))]), rules=done_rule).outcome
        == "eligible"
    )


def test_conflicting_assertions_are_reported_with_both_sources_and_not_resolved():
    pos = relative("mother", doc="doc-1", quote="Mother had the condition.")
    neg = relative("mother", assertion="negative", doc="doc-2", quote="Mother never had the condition.")
    res = ev(inp(facts=[pos, neg]), rules=one(exists("fam")))
    assert res.outcome == "insufficient_evidence"  # unknown, not "eligible because a positive exists"
    (conflict,) = res.conflicting_information
    assert conflict["type"] == "conflicting_assertions" and conflict["subject"] == "mother"
    assert {s["document_id"] for s in conflict["sources"]} == {"doc-1", "doc-2"}
    assert {s["quote"] for s in conflict["sources"]} == {
        "Mother had the condition.",
        "Mother never had the condition.",
    }
    other = relative("father", assertion="negative")  # a negative about a different relative is no conflict
    assert ev(inp(facts=[pos, other]), rules=one(exists("fam"))).outcome == "eligible"


def test_disagreeing_ages_at_onset_are_listed_and_never_picked_silently():
    a = relative("mother", onset=45, doc="doc-1", quote="Mother diagnosed at 45.")
    b = relative("mother", onset=62, doc="doc-2", quote="Mother diagnosed at 62.")
    early = one(lf("evidence.fam.min_age_at_onset", "LESS_THAN", 60))
    res = ev(inp(facts=[a, b]), rules=early)
    assert res.outcome == "insufficient_evidence"  # neither "early (45)" nor "late (62)" is chosen
    (conflict,) = res.conflicting_information
    assert conflict["type"] == "conflicting_age_at_onset" and conflict["values"] == [45, 62]
    assert {s["document_id"] for s in conflict["sources"]} == {"doc-1", "doc-2"}
    assert {s["quote"] for s in conflict["sources"]} == {"Mother diagnosed at 45.", "Mother diagnosed at 62."}
    assert (
        ev(inp(facts=[a, relative("mother", onset=45)]), rules=early).outcome == "eligible"
    )  # agreeing documents are fine
    # another relative with a known early age decides it despite the disagreement (interval upper bound)
    assert ev(inp(facts=[a, b, relative("father", onset=40)]), rules=early).outcome == "eligible"


def test_interval_aggregates_distinct_subjects_and_shared_subjects():
    two = one(lf("evidence.fam.distinct_subjects", "GREATER_THAN_OR_EQUAL", 2))
    assert ev(inp(facts=[relative("mother"), relative("father")]), rules=two).outcome == "eligible"
    assert (
        ev(inp(facts=[relative("mother"), relative("mother")]), rules=two).outcome == "not_eligible"
    )  # one person, two documents
    # one definite relative plus one UNRESOLVED relative: the count is the interval [1, 2] -> undecidable
    assert (
        ev(inp(facts=[relative("mother"), relative("father", assertion="uncertain")]), rules=two).outcome
        == "insufficient_evidence"
    )
    assert (
        ev(inp(facts=[relative("mother"), relative("cousin")]), rules=two).outcome == "insufficient_evidence"
    )
    one_or_more = one(lf("evidence.fam.distinct_subjects", "GREATER_THAN_OR_EQUAL", 1))
    assert (
        ev(
            inp(facts=[relative("mother"), relative("father", assertion="uncertain")]), rules=one_or_more
        ).outcome
        == "eligible"
    )
    shared = one(lf("evidence.fam.shared_subjects.fam_b", "GREATER_THAN_OR_EQUAL", 1))
    both = [relative("mother"), relative("mother", fact_type="other_condition"), relative("father")]
    assert ev(inp(facts=both), rules=shared).outcome == "eligible"  # the mother has both conditions
    assert (
        ev(
            inp(facts=[relative("mother"), relative("father", fact_type="other_condition")]), rules=shared
        ).outcome
        == "not_eligible"
    )
    disjoint = [relative("mother"), relative("father", fact_type="other_condition", assertion="uncertain")]
    assert (
        ev(inp(facts=disjoint), rules=shared).outcome == "not_eligible"
    )  # different people: no overlap even with the uncertain one
    maybe = [relative("mother"), relative("mother", fact_type="other_condition", assertion="uncertain")]
    assert (
        ev(inp(facts=maybe), rules=shared).outcome == "insufficient_evidence"
    )  # the same person MAY have both


def test_min_age_at_onset_and_early_start_age_follow_the_interval_convention():
    under_60 = one(lf("evidence.fam.min_age_at_onset", "LESS_THAN", 60))
    assert ev(inp(facts=[relative("mother", onset=55)]), rules=under_60).outcome == "eligible"
    assert ev(inp(facts=[relative("mother", onset=66)]), rules=under_60).outcome == "not_eligible"
    assert ev(inp(), rules=under_60).outcome == "not_eligible"  # no relatives: the minimum is +infinity
    # a relative with no age makes the minimum an interval (-inf, known]: a known early age still decides, a
    # late one cannot
    assert (
        ev(inp(facts=[relative("mother", onset=None), relative("father", onset=55)]), rules=under_60).outcome
        == "eligible"
    )
    assert (
        ev(inp(facts=[relative("mother", onset=None), relative("father", onset=66)]), rules=under_60).outcome
        == "insufficient_evidence"
    )
    assert (
        ev(
            inp(facts=[relative("father", onset=66), relative("mother", assertion="uncertain")]),
            rules=under_60,
        ).outcome
        == "insufficient_evidence"
    )
    # early_start_age = min(40, youngest onset - 10)
    start = lambda op, v: one(lf("evidence.fam.early_start_age", op, v))  # noqa: E731
    assert ev(inp(facts=[relative("mother", onset=55)]), rules=start("EQUALS", 40)).outcome == "eligible"
    assert (
        ev(
            inp(facts=[relative("mother", onset=55), relative("father", onset=38)]), rules=start("EQUALS", 28)
        ).outcome
        == "eligible"
    )
    assert (
        ev(
            inp(facts=[relative("mother", onset=55), relative("father", assertion="uncertain")]),
            rules=start("LESS_THAN", 99),
        ).outcome
        == "insufficient_evidence"
    )
    ref = one(lf("resident.age", "GREATER_THAN_OR_EQUAL", {"field": "evidence.fam.early_start_age"}))
    young = relative("father", onset=38)
    assert (
        ev(inp(person(birth=date(1999, 1, 1)), [young]), rules=ref).outcome == "not_eligible"
    )  # age 27 < early start 28
    assert (
        ev(inp(person(birth=date(1998, 1, 1)), [young]), rules=ref).outcome == "eligible"
    )  # age 28 reaches it
    assert (
        ev(inp(person(birth=date(1998, 1, 1)), [relative("father", onset=60)]), rules=ref).outcome
        == "not_eligible"
    )  # early start 40 > 28
    assert (
        ev(inp(person(birth=date(1980, 1, 1)), [relative("father", onset=None)]), rules=ref).outcome
        == "eligible"
    )  # 46 >= every possible start (<= 40)


def test_date_within_boundaries_partial_dates_and_interval_metadata():
    def window(dur):
        within = lf("evidence.done.latest_event_date", "DATE_WITHIN", dur)
        expr = {"operator": "AND", "conditions": [exists("done"), within]}
        return [rule("done", "temporal", 40, expr, FAIL_T, obr_when_true("already_completed"), "up_to_date")]

    r = window({"years": 5})
    assert (
        ev(inp(facts=[fact(start=date(2021, 11, 9))]), rules=r).outcome == "already_completed"
    )  # (eval - 5y, eval]
    assert ev(inp(facts=[fact(start=date(2021, 11, 8))]), rules=r).outcome == "eligible"
    assert ev(inp(facts=[fact(start=ON)]), rules=r).outcome == "already_completed"
    assert (
        ev(inp(facts=[fact(start=date(2026, 11, 9))]), rules=r).outcome == "eligible"
    )  # future-dated is not an event yet
    assert (
        ev(inp(facts=[fact(start=date(2024, 1, 1), end=date(2024, 12, 31))]), rules=r).outcome
        == "already_completed"
    )
    straddling = fact(
        start=date(2021, 1, 1), end=date(2021, 12, 31)
    )  # a year-only date across the window edge
    assert ev(inp(facts=[straddling]), rules=r).outcome == "insufficient_evidence"
    assert (
        ev(inp(facts=[straddling, fact(start=date(2025, 5, 5))]), rules=r).outcome == "already_completed"
    )  # another fact satisfies it
    assert ev(inp(facts=[fact(start=date(2015, 1, 1))]), rules=r).outcome == "eligible"
    # an interval recommendation (1-3 years): the maximum decides when basis is "max"
    ranged = window({"years": 3, "range_years": [1, 3], "basis": "max"})
    assert ev(inp(facts=[fact(start=date(2023, 12, 1))]), rules=ranged).outcome == "already_completed"
    assert ev(inp(facts=[fact(start=date(2023, 11, 1))]), rules=ranged).outcome == "eligible"


def test_structured_records_become_facts_by_code():
    rec = record_to_fact(
        uuid.uuid4(),
        "procedure",
        "http://snomed.info/sct",
        "111",
        date(2025, 1, 1),
        None,
        "completed",
        ON,
        "test://p",
    )
    assert rec is not None and rec.code_system == "SNOMED-CT" and rec.temporal == "historical"
    r = [rule("done", "temporal", 40, exists("done"), FAIL_T, obr_when_true("already_completed"), "done")]
    assert ev(inp(facts=[rec]), rules=r).outcome == "already_completed"
    assert (
        record_to_fact(
            uuid.uuid4(),
            "procedure",
            "http://snomed.info/sct",
            "111",
            date(2025, 1, 1),
            None,
            "entered-in-error",
            ON,
        )
        is None
    )
    planned = record_to_fact(
        uuid.uuid4(), "procedure", "http://snomed.info/sct", "111", date(2027, 1, 1), None, "completed", ON
    )
    assert planned.temporal == "planned" and ev(inp(facts=[planned]), rules=r).outcome == "eligible"
    other_system = record_to_fact(
        uuid.uuid4(), "procedure", "http://other", "111", date(2025, 1, 1), None, "completed", ON
    )
    assert (
        ev(inp(facts=[other_system]), rules=r).outcome == "eligible"
    )  # the same code in another system is a different code
    ongoing = record_to_fact(uuid.uuid4(), "condition", "x", "1", date(2020, 1, 1), None, "active", ON)
    resolved = record_to_fact(
        uuid.uuid4(), "condition", "x", "1", date(2020, 1, 1), date(2021, 1, 1), "resolved", ON
    )
    assert (ongoing.temporal, resolved.temporal) == ("current", "historical")


def test_coverage_window():
    assert ev(inp(covered=False)).reason_code == "no_coverage"
    assert (
        ev(ResidentInputs(person(), (), date(2020, 1, 1))).reason_code == "no_coverage"
    )  # last record older than 5 years
    assert (
        ev(ResidentInputs(person(), (), shift_back(ON, 5))).reason_code == "no_coverage"
    )  # exclusive lower bound
    assert ev(ResidentInputs(person(), (), ON)).outcome == "eligible"


# ---------------------------------------------------------------------------------------------- determinism
# and hashing
def _rich():
    facts = [
        relative("mother", onset=50, doc="d1", quote="q"),
        fact(start=date(2015, 1, 1)),
        fact("condition", "other_condition", "resident"),
        relative("father", assertion="negative"),
    ]
    return person(date(1975, 3, 3)), facts


def test_identical_inputs_give_identical_results_and_order_does_not_matter():
    d = defn()
    res, facts = _rich()
    baseline = json.dumps(
        evaluate_guideline(d, inp(res, facts), ON, SOURCES).explanation({"k": 1}, ON), sort_keys=True
    )
    deps = GuidelineDeps.of(d)
    base_hash = input_hash(deps, "h", inp(res, facts), ON)
    rng = random.Random(7)  # noqa: S311 - a seeded shuffle for the order-independence test, not security
    for _ in range(40):
        rng.shuffle(facts)
        i = inp(res, facts)
        assert (
            json.dumps(evaluate_guideline(d, i, ON, SOURCES).explanation({"k": 1}, ON), sort_keys=True)
            == baseline
        )
        assert input_hash(deps, "h", i, ON) == base_hash


def test_input_hash_tracks_exactly_the_relevant_inputs():
    d = defn()
    deps = GuidelineDeps.of(d)
    res, facts = _rich()
    base = input_hash(deps, "h", inp(res, facts), ON)
    irrelevant = [
        fact("symptom", "unrelated", "resident"),
        fact("condition", "unrelated_condition", "resident", validation="rejected"),
    ]
    assert input_hash(deps, "h", inp(res, facts + irrelevant), ON) == base
    assert (
        input_hash(deps, "h", ResidentInputs(res, tuple(facts), date(2026, 7, 1)), ON) == base
    )  # still covered: same hash
    assert input_hash(deps, "h", inp(res, facts + [fact(start=date(2025, 1, 1))]), ON) != base
    assert (
        input_hash(deps, "h", inp(res, facts + [fact("condition", "bad_condition", "resident")]), ON) != base
    )  # feeds an exclusion
    assert input_hash(deps, "h", inp(res, facts), date(2026, 11, 9)) != base
    assert input_hash(deps, "other", inp(res, facts), ON) != base
    assert input_hash(deps, "h", inp(person(date(1975, 3, 4)), facts), ON) != base
    assert input_hash(deps, "h", inp(res, facts, covered=False), ON) != base  # coverage flipped


def test_sex_matters_to_the_hash_only_when_a_rule_uses_it():
    a = person(sex="female")
    b = ResidentFacts(a.id, a.birth_date, None, "male")
    deps = GuidelineDeps.of(defn())
    assert input_hash(deps, "h", inp(a), ON) == input_hash(deps, "h", inp(b), ON)
    sex_rule = one(lf("resident.sex_assigned_at_birth", "EQUALS", "female"))
    sd = GuidelineDeps.of(defn(sex_rule))
    assert input_hash(sd, "h", inp(a), ON) != input_hash(sd, "h", inp(b), ON)


def test_engine_has_no_clock_randomness_network_or_model_dependencies():
    """08 §8.2 / §16.4 #10: evaluation is a pure function of rules + inputs + explicit date; no LLM can be
    involved."""  # noqa: E501
    forbidden = {
        "random",
        "time",
        "httpx",
        "requests",
        "anthropic",
        "openai",
        "socket",
        "subprocess",
        "sqlalchemy",
    }
    for mod in (evaluator_module, __import__("anticipa.guidelines.rules", fromlist=["x"])):
        source = Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
                assert not node.module.startswith("anticipa.ai"), node.module
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "compile", "__import__"}, (
                    mod.__name__,
                    node.func.id,
                )
        assert not imported & forbidden, (mod.__name__, imported & forbidden)
        for token in ("datetime.now", "date.today", "utcnow"):
            assert token not in source, (mod.__name__, token)
