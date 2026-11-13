"""Rule format validation (08 §6.3): unknown operators/fields/keys are rejected, field contracts are enforced,
rule
metadata cannot reach the evaluator, and canonical hashes are stable. Pure unit tests, no database."""

from __future__ import annotations

import copy

import pytest

from anticipa.guidelines.rules import (
    MAX_DEPTH,
    MAX_LEAVES,
    RuleValidationError,
    parse_guideline_definition,
    parse_rule,
)

SEL = {
    "done": {
        "category_in": ["procedure"],
        "fact_type_in": ["thing"],
        "codes": [],
        "subject_type_in": ["resident"],
        "assertion_status_in": ["positive"],
        "temporal_status_in": ["current", "historical"],
    },
    "fam": {
        "category_in": ["family_history"],
        "fact_type_in": ["thing"],
        "subject_type_in": ["family_member"],
        "assertion_status_in": ["positive"],
        "temporal_status_in": ["current", "historical", "unknown"],
        "relationship_in": ["mother"],
    },
}
SEM = {"true": "passed", "false": "failed", "unknown": "unknown"}
OBR = {"true": None, "false": "not_eligible", "unknown": "insufficient_evidence"}


def leaf(field="resident.age", operator="GREATER_THAN_OR_EQUAL", value=40):
    return {"field": field, "operator": operator, "value": value}


def rule(expression=None, rid="g.r1", **kw):
    return {
        "rule_id": rid,
        "name": "r",
        "rule_type": "inclusion",
        "priority": 10,
        "expression": expression or leaf(),
        "criterion_semantics": SEM,
        "outcome_by_expression_result": OBR,
        "reason_code": "why",
        **kw,
    }


def defn(*rules, selectors=SEL, policy=None):
    return parse_guideline_definition(list(rules), selectors, policy)


def errors_of(*rules, selectors=SEL, policy=None) -> str:
    with pytest.raises(RuleValidationError) as exc:
        defn(*rules, selectors=selectors, policy=policy)
    return " | ".join(f"{e['field']}: {e['reason']}" for e in exc.value.errors)


def test_valid_definition_parses_with_default_policy():
    d = defn(rule({"operator": "AND", "conditions": [leaf(), leaf("evidence.selector", "EXISTS", "done")]}))
    assert d.decision_policy.steps[-1].otherwise == "eligible"
    assert d.rules[0].key == "r1" and d.rules[0].selectors_used() == {"done"}


def test_content_hash_is_independent_of_key_order_and_changes_with_content():
    a = parse_rule(rule())
    b = parse_rule(dict(reversed(list(rule().items()))))
    assert a.content_hash() == b.content_hash()
    changed = rule(leaf(value=41))
    assert parse_rule(changed).content_hash() != a.content_hash()


def test_metadata_is_kept_but_is_never_an_engine_key():
    r = parse_rule(rule(source_refs=["x#E1"], grade="B", applicability={"sex": "female"}, notes="free text"))
    assert r.model_extra["grade"] == "B" and r.normalized()["notes"] == "free text"
    # metadata participates in the content hash (so editing it creates a new version) but not in evaluation
    assert r.content_hash() != parse_rule(rule()).content_hash()


@pytest.mark.parametrize("operator", ["CONTAINS", "REGEX", "eq", "DATE_BEFORE", "__import__('os')", ""])
def test_unknown_operator_rejected(operator):
    assert errors_of(rule(leaf(operator=operator)))


@pytest.mark.parametrize(
    "field",
    [
        "resident.income",
        "resident.sex",
        "evidence.done.average",
        "evidence.done.count",
        "evidence.done",
        "eval(1)",
        "evidence..x",
        "",
    ],
)
def test_unknown_field_rejected(field):
    assert errors_of(rule(leaf(field=field)))


def test_unknown_keys_rejected_inside_expressions():
    assert errors_of(rule({**leaf(), "python": "1+1"}))
    assert errors_of(rule({"operator": "AND", "conditions": [leaf()], "script": "x"}))
    assert errors_of(rule({"operator": "NOT", "condition": leaf(), "extra": 1}))


def test_code_like_strings_are_data_not_code():
    assert "resident.sex_assigned_at_birth" in errors_of(
        rule(leaf("resident.sex_assigned_at_birth", "EQUALS", "__import__('os').system('x')"))
    )
    assert "selector" in errors_of(rule(leaf("evidence.selector", "EXISTS", "__import__('os')")))


def test_operator_must_suit_the_field():
    assert "not valid for field" in errors_of(rule(leaf("resident.age", "EXISTS", None)))
    assert "not valid for field" in errors_of(rule(leaf("evidence.selector", "GREATER_THAN", "done")))
    assert "DATE_WITHIN" in errors_of(rule(leaf("evidence.done.latest_event_date", "GREATER_THAN", 3)))
    assert "duration" in errors_of(rule(leaf("evidence.done.latest_event_date", "DATE_WITHIN", 5)))
    assert "longer than zero" in errors_of(
        rule(leaf("evidence.done.latest_event_date", "DATE_WITHIN", {"years": 0}))
    )


def test_values_are_typed():
    assert errors_of(rule(leaf(value=True)))  # booleans are not numbers
    assert errors_of(rule(leaf(value="forty")))
    assert errors_of(rule(leaf(operator="IN", value=40)))
    assert errors_of(rule(leaf("resident.sex_assigned_at_birth", "EQUALS", "robot")))
    assert errors_of(rule(leaf("resident.sex_assigned_at_birth", "IN", "female")))
    defn(rule(leaf("resident.sex_assigned_at_birth", "IN", ["female", "unknown"])))


def test_interval_duration_metadata_must_be_consistent():
    ok = {"years": 3, "range_years": [1, 3], "basis": "max"}
    defn(rule(leaf("evidence.done.latest_event_date", "DATE_WITHIN", ok)))
    for bad in (
        {"years": 2, "range_years": [1, 3], "basis": "max"},
        {"years": 3, "range_years": [1, 3]},
        {"years": 3, "basis": "max"},
        {"years": 3, "range_years": [3, 1], "basis": "max"},
        {"years": 3, "range_years": [1, 3], "basis": "min"},
    ):
        assert errors_of(rule(leaf("evidence.done.latest_event_date", "DATE_WITHIN", bad))), bad


def test_field_references_must_point_at_numeric_aggregates():
    ok = rule(leaf(value={"field": "evidence.fam.min_age_at_onset"}))
    defn(ok)
    defn(rule(leaf(value={"field": "evidence.fam.early_start_age"})))
    assert "numeric evidence aggregate" in errors_of(
        rule(leaf(value={"field": "evidence.fam.latest_event_date"}))
    )
    assert errors_of(rule(leaf(value={"field": "resident.sex"})))


def test_unknown_selector_reference_rejected():
    assert "unknown selectors" in errors_of(rule(leaf("evidence.selector", "EXISTS", "nope")))
    assert "unknown selectors" in errors_of(rule(leaf("evidence.nope.distinct_subjects", "GREATER_THAN", 1)))
    defn(rule(leaf("evidence.done.shared_subjects.fam", "GREATER_THAN_OR_EQUAL", 1)))
    assert "unknown selectors" in errors_of(
        rule(leaf("evidence.done.shared_subjects.nope", "GREATER_THAN_OR_EQUAL", 1))
    )


def test_not_takes_exactly_one_child():
    defn(rule({"operator": "NOT", "condition": leaf()}))
    defn(rule({"operator": "NOT", "conditions": [leaf()]}))  # the research drafts write it this way
    assert errors_of(rule({"operator": "NOT", "conditions": [leaf(), leaf()]}))
    assert errors_of(rule({"operator": "NOT"}))


def test_criterion_semantics_and_vocabularies_are_closed():
    assert errors_of(rule(criterion_semantics={"true": "passed", "false": "maybe", "unknown": "unknown"}))
    assert errors_of(rule(criterion_semantics={"true": "passed", "false": "failed"}))
    assert errors_of(
        rule(outcome_by_expression_result={"true": "approved_anyway", "false": None, "unknown": None})
    )
    assert errors_of(rule(outcome_by_expression_result={"true": None, "false": None}))
    assert errors_of(rule(rule_type="magic"))
    assert errors_of(rule(missing_data_policy="guess"))
    assert errors_of(rule(priority="high"))


def test_decision_policy_shape_is_validated():
    bad = {"steps": [{"rank": 1, "outcome": "not_eligible"}]}
    assert "last decision step" in errors_of(rule(), policy=bad)
    assert errors_of(rule(), policy={"steps": [{"otherwise": "eligible"}, {"otherwise": "eligible"}]})
    assert errors_of(rule(), policy={"steps": [{"rank": 1, "outcome": "yes"}, {"otherwise": "eligible"}]})
    assert "ascending" in errors_of(
        rule(),
        policy={
            "steps": [
                {"rank": 2, "outcome": "not_eligible"},
                {"rank": 1, "outcome": "insufficient_evidence"},
                {"otherwise": "eligible"},
            ]
        },
    )
    assert errors_of(
        rule(),
        policy={
            "steps": [
                {"rank": 1, "outcome": "not_eligible", "otherwise": "eligible"},
                {"otherwise": "eligible"},
            ]
        },
    )
    assert "no decision step ranks it" in errors_of(
        rule(), policy={"steps": [{"rank": 1, "outcome": "insufficient_evidence"}, {"otherwise": "eligible"}]}
    )


def test_duplicate_rule_ids_and_keys_rejected():
    assert "unique" in errors_of(rule(rid="g.same"), rule(rid="g.same"))
    assert "keys must be unique" in errors_of(rule(rid="a.same"), rule(rid="b.same"))
    assert "reserved" in errors_of(rule(rid="engine.error"))


def test_depth_and_size_limits():
    node = leaf()
    for _ in range(MAX_DEPTH + 1):
        node = {"operator": "NOT", "condition": node}
    assert "deeper" in errors_of(rule(node))
    big = {"operator": "OR", "conditions": [leaf(operator="EQUALS", value=i) for i in range(MAX_LEAVES + 1)]}
    assert "at most" in errors_of(rule(big))
    deep: dict = {"x": 1}
    for _ in range(500):
        deep = {"x": deep}
    assert "nested too deeply" in errors_of(deep)


def test_non_objects_rejected():
    with pytest.raises(RuleValidationError):
        parse_rule([1, 2])
    with pytest.raises(RuleValidationError):
        parse_rule("expression")


def test_selector_must_be_complete():
    broken = copy.deepcopy(SEL)
    del broken["done"]["category_in"]
    assert "selectors.done" in errors_of(rule(leaf("evidence.selector", "EXISTS", "done")), selectors=broken)
    assert errors_of(rule(), selectors={"Bad Name": SEL["done"]})
