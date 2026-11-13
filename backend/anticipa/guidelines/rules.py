"""Declarative rules in the Docs/08 §6 format (the format of config/guidelines/rules_draft.json, schema v2):
constrained
JSON, pydantic-validated, never executed. `rule_language = "anticipa-08-v1"`.

A guideline version is evaluated as a whole (08 §8): every rule turns its expression result (true / false /
unknown) into
a criterion outcome (`criterion_semantics`) and into an overall-outcome contribution
(`outcome_by_expression_result`,
null = none); the version's `decision_policy` ranks the contributions and the best-ranked one is the overall
outcome
(none -> eligible). The seven overall outcomes are those of 04 §11.2.

Rule (engine-executed keys; every other key — source references, grade, applicability, explanation text,
pathways, notes —
is stored verbatim as metadata and can never influence evaluation):
    rule_id, name, rule_type, priority (int; the lowest number wins when naming the primary reason),
    expression,
    criterion_semantics, outcome_by_expression_result, review_class (optional; lets a decision step rank only
    some
    requires_clinical_review contributions), missing_data_policy (unknown | not_satisfied | review_required),
    reason_code, reason_code_when_unknown, explanation_templates (approved text; never LLM-written).

Expression nodes (unknown operators/fields/keys are rejected):
    {"operator": "AND"|"OR", "conditions": [...]}
    {"operator": "NOT", "condition": {...}}  (or a 1-item "conditions")
    {"field", "operator", "value"} leaves:
      resident.age                     whole years at the evaluation date; numeric operators;
                                       value int or {"field": agg}
      resident.sex_assigned_at_birth   EQUALS NOT_EQUALS IN NOT_IN with female|male|unknown
      evidence.selector                EXISTS | NOT_EXISTS, value = selector name from the version's library
      evidence.<sel>.latest_event_date DATE_WITHIN, value {"years"|"months"|"days": n, optional
                                       "range_years": [min, max], "basis": "max"}: the event date lies in
                                       (evaluation_date - n, evaluation_date];
                                                partial dates use their full range; a range is metadata,
                                                `years` (the max) decides
      evidence.<sel>.distinct_subjects | min_age_at_onset | early_start_age | shared_subjects.<other sel>
                                                numeric operators; value int or {"field": "..."}; aggregates
                                                are INTERVALS when
                                                subjects are unresolved or ages missing/conflicting (07
                                                aggregate_semantics)

Selectors pick evidence by category, fact_type / codes, subject_type, relationship (and
`unresolved_relationship_in`),
assertion and temporal status. Only facts with validation_status `accepted` are ever definite (07, 08 §10.1).
"""

from __future__ import annotations

import hashlib
import re
from functools import cached_property
from typing import Annotated, Any, Literal

import orjson
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    StrictInt,
    StrictStr,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)

RULE_LANGUAGE = "anticipa-08-v1"
INPUT_SCHEMA_VERSION = "3"

MAX_DEPTH = 8
MAX_LEAVES = 64
MAX_RULES = 80

MissingPolicy = Literal["unknown", "not_satisfied", "review_required"]
CriterionOutcome = Literal["passed", "failed", "unknown", "not_applicable", "error"]
# The seven overall outcomes of 04 §11.2.
OUTCOMES = (
    "eligible",
    "not_eligible",
    "already_completed",
    "insufficient_evidence",
    "requires_clinical_review",
    "temporarily_excluded",
    "evaluation_error",
)
Outcome = Literal[OUTCOMES]  # type: ignore[valid-type]
OUTCOME_CLASSES = ("eligible", "ineligible", "indeterminate", "error")  # 08 §7 classes
OUTCOME_CLASS: dict[str, str] = {
    "eligible": "eligible",
    "not_eligible": "ineligible",
    "already_completed": "ineligible",
    "temporarily_excluded": "ineligible",
    "insufficient_evidence": "indeterminate",
    "requires_clinical_review": "indeterminate",
    "evaluation_error": "error",
}
RULE_TYPES = ("prerequisite", "inclusion", "exclusion", "temporal")
NUMERIC_OPS = frozenset(
    {
        "EQUALS",
        "NOT_EQUALS",
        "GREATER_THAN",
        "GREATER_THAN_OR_EQUAL",
        "LESS_THAN",
        "LESS_THAN_OR_EQUAL",
        "IN",
        "NOT_IN",
    }
)
SET_OPS = frozenset({"EQUALS", "NOT_EQUALS", "IN", "NOT_IN"})
PRESENCE_OPS = frozenset({"EXISTS", "NOT_EXISTS"})
NUMERIC_AGGREGATES = ("distinct_subjects", "min_age_at_onset", "early_start_age")
DATE_AGGREGATES = ("latest_event_date",)
SEXES = ("male", "female", "unknown")
RESERVED_IDS = frozenset({"engine.resident_alive", "engine.error"})
_NAME = r"[a-z][a-z0-9_]{0,63}"
_NAME_RE = rf"^{_NAME}$"
_AGG_FIELD = re.compile(rf"^evidence\.({_NAME})\.({'|'.join(NUMERIC_AGGREGATES + DATE_AGGREGATES)})$")
_SHARED_FIELD = re.compile(rf"^evidence\.({_NAME})\.shared_subjects\.({_NAME})$")
AGE_FIELD = "resident.age"
SEX_FIELD = "resident.sex_assigned_at_birth"

# Genetic degree of relationship (a definition, not a clinical criterion).
RELATIONSHIP_DEGREE: dict[str, int] = {
    "mother": 1,
    "father": 1,
    "sister": 1,
    "brother": 1,
    "daughter": 1,
    "son": 1,
    "maternal_grandmother": 2,
    "maternal_grandfather": 2,
    "paternal_grandmother": 2,
    "paternal_grandfather": 2,
    "maternal_aunt": 2,
    "maternal_uncle": 2,
    "paternal_aunt": 2,
    "paternal_uncle": 2,
    "half_sister": 2,
    "half_brother": 2,
    "cousin": 3,
}


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Duration(_Closed):
    years: int = Field(0, ge=0, le=120)
    months: int = Field(0, ge=0, le=1440)
    days: int = Field(0, ge=0, le=36500)
    range_years: list[StrictInt] | None = Field(
        None, min_length=2, max_length=2
    )  # (min, max): recommended interval range
    basis: Literal["max"] | None = (
        None  # which end of the range decides: the maximum (due date = last event + max)
    )

    @model_validator(mode="after")
    def _consistent(self) -> Duration:
        if self.years + self.months + self.days <= 0:
            raise ValueError("a duration must be longer than zero")
        if self.range_years is not None:
            lo, hi = self.range_years
            if not 0 < lo <= hi:
                raise ValueError("range_years must be [min, max] with 0 < min <= max")
            if self.basis != "max" or self.years != hi or self.months or self.days:
                raise ValueError(
                    'with range_years, basis must be "max" and years must equal the range maximum'
                )
        elif self.basis is not None:
            raise ValueError("basis is only meaningful together with range_years")
        return self


class FieldRef(_Closed):
    field: str


def parse_field(name: str) -> tuple[str, str, str | None] | None:
    """(selector, aggregate, other selector for shared_subjects) of an evidence aggregate field, else None."""
    if m := _AGG_FIELD.match(name):
        return m.group(1), m.group(2), None
    if m := _SHARED_FIELD.match(name):
        return m.group(1), "shared_subjects", m.group(2)
    return None


class Leaf(_Closed):
    field: str
    operator: Literal[
        "EQUALS",
        "NOT_EQUALS",
        "GREATER_THAN",
        "GREATER_THAN_OR_EQUAL",
        "LESS_THAN",
        "LESS_THAN_OR_EQUAL",
        "IN",
        "NOT_IN",
        "EXISTS",
        "NOT_EXISTS",
        "DATE_WITHIN",
    ]
    value: StrictInt | StrictStr | list[StrictInt] | list[StrictStr] | Duration | FieldRef | None = None

    @cached_property
    def parsed_field(self) -> tuple[str, str, str | None] | None:
        return parse_field(self.field)

    @model_validator(mode="after")
    def _contract(self) -> Leaf:
        op, v = self.operator, self.value
        if self.field == AGE_FIELD:
            self._ops(NUMERIC_OPS)
            self._numeric_value()
        elif self.field == SEX_FIELD:
            self._ops(SET_OPS)
            items = v if isinstance(v, list) else [v]
            if (
                (op in ("IN", "NOT_IN")) != isinstance(v, list)
                or not items
                or not all(isinstance(x, str) and x in SEXES for x in items)
            ):
                raise ValueError(
                    f"{op} on {SEX_FIELD} needs {'a list' if op in ('IN', 'NOT_IN') else 'one value'} from {list(SEXES)}"  # noqa: E501
                )
        elif self.field == "evidence.selector":
            self._ops(PRESENCE_OPS)
            if not isinstance(v, str) or not re.match(_NAME_RE, v):
                raise ValueError("evidence.selector needs a selector name as value")
        elif (pf := parse_field(self.field)) is not None:
            if pf[1] in DATE_AGGREGATES:
                if op != "DATE_WITHIN":
                    raise ValueError(f"{self.field} supports DATE_WITHIN only")
                if not isinstance(v, Duration):
                    raise ValueError('DATE_WITHIN needs a duration value like {"years": 5}')
            else:
                self._ops(NUMERIC_OPS)
                self._numeric_value()
        else:
            raise ValueError(f"unknown field '{self.field}'")
        return self

    def _ops(self, ops: frozenset[str]) -> None:
        if self.operator not in ops:
            raise ValueError(
                f"operator {self.operator} is not valid for field {self.field}; use one of {sorted(ops)}"
            )

    def _numeric_value(self) -> None:
        v = self.value
        if self.operator in ("IN", "NOT_IN"):
            if not isinstance(v, list) or not v or not all(isinstance(x, int) for x in v):
                raise ValueError(f"{self.operator} needs a non-empty list of integers")
        elif isinstance(v, FieldRef):
            pf = parse_field(v.field)
            if pf is None or pf[1] in DATE_AGGREGATES:
                raise ValueError(f"value reference '{v.field}' is not a numeric evidence aggregate")
        elif not isinstance(v, int):
            raise ValueError(f"{self.operator} needs an integer value or a field reference")


class AndOrGroup(_Closed):
    operator: Literal["AND", "OR"]
    conditions: list[Node] = Field(min_length=1)


class NotGroup(_Closed):
    operator: Literal["NOT"]
    condition: Node | None = None
    conditions: list[Node] | None = Field(None, min_length=1, max_length=1)

    @model_validator(mode="after")
    def _one_child(self) -> NotGroup:
        if (self.condition is None) == (self.conditions is None):
            raise ValueError("NOT needs exactly one of `condition` or a single-item `conditions`")
        return self

    @property
    def child(self) -> Node:
        return self.condition if self.condition is not None else self.conditions[0]  # type: ignore[index]


def _node_tag(value: Any) -> str:
    op = value.get("operator") if isinstance(value, dict) else getattr(value, "operator", None)
    if op in ("AND", "OR"):
        return "group"
    if op == "NOT":
        return "not"
    return "leaf"


Node = Annotated[
    Annotated[AndOrGroup, Tag("group")] | Annotated[NotGroup, Tag("not")] | Annotated[Leaf, Tag("leaf")],
    Discriminator(_node_tag),
]


def leaves_of(node: Any) -> list[Leaf]:
    if isinstance(node, Leaf):
        return [node]
    if isinstance(node, NotGroup):
        return leaves_of(node.child)
    return [lf for c in node.conditions for lf in leaves_of(c)]


class CriterionSemantics(_Closed):
    true: CriterionOutcome
    false: CriterionOutcome
    unknown: CriterionOutcome

    def for_result(self, value: bool | None) -> str:
        return self.unknown if value is None else (self.true if value else self.false)


class OutcomeMap(_Closed):
    """Expression result -> contribution to the overall outcome (null = none)."""

    true: Outcome | None
    false: Outcome | None
    unknown: Outcome | None

    def for_result(self, value: bool | None) -> str | None:
        return self.unknown if value is None else (self.true if value else self.false)


class Rule(BaseModel):
    """An 08 §6.1 rule. Unknown keys are kept as metadata (`extra="allow"`) but are never executed."""

    model_config = ConfigDict(extra="allow")

    rule_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    rule_type: Literal["prerequisite", "inclusion", "exclusion", "temporal"]
    priority: StrictInt = Field(ge=0, le=100000)
    expression: Node
    criterion_semantics: CriterionSemantics
    outcome_by_expression_result: OutcomeMap
    missing_data_policy: MissingPolicy = "unknown"
    reason_code: str = Field(pattern=_NAME_RE)
    reason_code_when_unknown: str | None = Field(None, pattern=_NAME_RE)
    review_class: str | None = Field(None, pattern=_NAME_RE)
    explanation_templates: dict[Literal["true", "false", "unknown"], str] | None = None

    @field_validator("rule_id")
    @classmethod
    def _id(cls, v: str) -> str:
        if v in RESERVED_IDS:
            raise ValueError("reserved rule id")
        return v

    @model_validator(mode="after")
    def _structure(self) -> Rule:
        def depth(node: Any) -> int:
            if isinstance(node, Leaf):
                return 1
            if isinstance(node, NotGroup):
                return 1 + depth(node.child)
            return 1 + max(depth(c) for c in node.conditions)

        if depth(self.expression) > MAX_DEPTH:
            raise ValueError(f"expression nesting is deeper than {MAX_DEPTH}")
        if len(leaves_of(self.expression)) > MAX_LEAVES:
            raise ValueError(f"a rule may have at most {MAX_LEAVES} conditions")
        return self

    @property
    def key(self) -> str:
        """Short rule key: the part of rule_id after the first '.' (guideline prefix), else the whole id."""
        return self.rule_id.split(".", 1)[1] if "." in self.rule_id else self.rule_id

    def selectors_used(self) -> set[str]:
        names: set[str] = set()
        for lf in leaves_of(self.expression):
            if lf.field == "evidence.selector":
                names.add(str(lf.value))
            elif lf.parsed_field:
                names.add(lf.parsed_field[0])
                if lf.parsed_field[2]:
                    names.add(lf.parsed_field[2])
            if isinstance(lf.value, FieldRef) and (pf := parse_field(lf.value.field)):
                names.add(pf[0])
                if pf[2]:
                    names.add(pf[2])
        return names

    def possible_contributions(self) -> set[str]:
        out = {
            o
            for o in (
                self.outcome_by_expression_result.true,
                self.outcome_by_expression_result.false,
                self.outcome_by_expression_result.unknown,
            )
            if o
        }
        if self.missing_data_policy == "review_required":
            out.add("requires_clinical_review")
        return out

    def normalized(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False, exclude_unset=False)

    def content_hash(self) -> str:
        return canonical_hash(self.normalized())


class Selector(BaseModel):
    model_config = ConfigDict(extra="allow")

    category_in: list[str] = Field(min_length=1)
    fact_type_in: list[str] = Field(min_length=1)
    codes: list[dict[str, Any]] = Field(default_factory=list)
    subject_type_in: list[Literal["resident", "family_member", "other", "unknown"]] = Field(min_length=1)
    assertion_status_in: list[Literal["positive", "negative", "uncertain", "conditional"]] = Field(
        min_length=1
    )
    temporal_status_in: list[Literal["current", "historical", "planned", "unknown"]] = Field(min_length=1)
    relationship_in: list[str] | None = None
    unresolved_relationship_in: list[str] | None = (
        None  # relatives whose relevance cannot be decided (e.g. sex unknown)
    )
    lookback_years: int | None = Field(None, ge=1, le=120)

    @cached_property
    def codeset(self) -> frozenset[tuple[str, str]]:
        return frozenset(
            (str(c.get("system")), str(c.get("code")))
            for c in self.codes
            if c.get("system") and c.get("code")
        )

    @cached_property
    def is_coverage(self) -> bool:
        return "any" in self.category_in and "any" in self.fact_type_in

    def normalized(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class DecisionStep(BaseModel):
    """`{"rank": n, "outcome": o, "only_rules_with_review_class": c?}` or the closing `{"otherwise":
    "eligible"}`."""  # noqa: E501

    model_config = ConfigDict(extra="forbid")
    rank: StrictInt | None = Field(None, ge=1)
    outcome: Outcome | None = None
    only_rules_with_review_class: str | None = Field(None, pattern=_NAME_RE)
    otherwise: Outcome | None = None

    @model_validator(mode="after")
    def _shape(self) -> DecisionStep:
        if self.otherwise is not None:
            if (
                self.rank is not None
                or self.outcome is not None
                or self.only_rules_with_review_class is not None
            ):
                raise ValueError("an `otherwise` step has no rank, outcome or class")
        elif self.rank is None or self.outcome is None:
            raise ValueError("a step needs rank and outcome, or otherwise")
        return self


class DecisionPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = "precedence_v2"
    steps: list[DecisionStep] = Field(min_length=1)

    @model_validator(mode="after")
    def _ranked(self) -> DecisionPolicy:
        if self.steps[-1].otherwise is None:
            raise ValueError("the last decision step must be `otherwise`")
        ranked = self.steps[:-1]
        if any(s.otherwise is not None for s in ranked):
            raise ValueError("`otherwise` is only allowed as the last step")
        ranks = [s.rank for s in ranked]
        if ranks != sorted(set(ranks)):
            raise ValueError("decision step ranks must be unique and ascending")
        return self

    def matching_step(self, outcome: str, review_class: str | None) -> DecisionStep | None:
        for step in self.steps[:-1]:
            if step.outcome == outcome and (step.only_rules_with_review_class in (None, review_class)):
                return step
        return None


DEFAULT_DECISION_POLICY: dict[str, Any] = {
    "name": "precedence_v2",
    "steps": [
        {"rank": 1, "outcome": "not_eligible"},
        {"rank": 2, "outcome": "requires_clinical_review"},
        {"rank": 3, "outcome": "already_completed"},
        {"rank": 4, "outcome": "temporarily_excluded"},
        {"rank": 5, "outcome": "evaluation_error"},
        {"rank": 6, "outcome": "insufficient_evidence"},
        {"otherwise": "eligible"},
    ],
}


class GuidelineDefinition(BaseModel):
    """Everything the engine needs to evaluate one guideline version."""

    rules: list[Rule] = Field(min_length=1, max_length=MAX_RULES)
    selectors: dict[str, Selector]
    decision_policy: DecisionPolicy

    @model_validator(mode="after")
    def _consistent(self) -> GuidelineDefinition:
        ids = [r.rule_id for r in self.rules]
        if len(set(ids)) != len(ids):
            raise ValueError("rule ids must be unique")
        keys = [r.key for r in self.rules]
        if len(set(keys)) != len(keys):
            raise ValueError("rule keys must be unique")
        missing = sorted({s for r in self.rules for s in r.selectors_used()} - set(self.selectors))
        if missing:
            raise ValueError(f"rules reference unknown selectors: {missing}")
        for (
            r
        ) in self.rules:  # a contribution no decision step ranks would be silently ignored: reject instead
            for outcome in sorted(r.possible_contributions()):
                if (
                    outcome != "eligible"
                    and self.decision_policy.matching_step(outcome, r.review_class) is None
                ):
                    raise ValueError(
                        f"rule {r.rule_id} can contribute '{outcome}' (review_class={r.review_class}) but no decision step ranks it"  # noqa: E501
                    )
        return self


class RuleValidationError(ValueError):
    """`.errors` is a list of {"field", "reason"} dicts safe to return to clients."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("; ".join(f"{e['field']}: {e['reason']}" for e in errors))
        self.errors = errors


def _json_depth_exceeds(value: Any, limit: int) -> bool:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        v, d = stack.pop()
        if d > limit:
            return True
        if isinstance(v, dict):
            stack.extend((x, d + 1) for x in v.values())
        elif isinstance(v, list):
            stack.extend((x, d + 1) for x in v)
    return False


def _errors(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {"field": ".".join(str(p) for p in e["loc"]) or "$", "reason": e["msg"]}
        for e in exc.errors(include_url=False, include_input=False)
    ][:50]


def _parse(model: type[BaseModel], raw: Any, what: str) -> Any:
    if not isinstance(raw, dict):
        raise RuleValidationError([{"field": "$", "reason": f"a {what} must be a JSON object"}])
    if _json_depth_exceeds(raw, MAX_DEPTH * 3 + 8):
        raise RuleValidationError([{"field": "$", "reason": f"{what} is nested too deeply"}])
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise RuleValidationError(_errors(exc)) from exc


def parse_rule(raw: Any) -> Rule:
    """Validate one untrusted rule object (selector references are checked at guideline level)."""
    return _parse(Rule, raw, "rule")


def parse_selector(raw: Any) -> Selector:
    return _parse(Selector, raw, "selector")


def parse_guideline_definition(
    rules: list[Any], selectors: dict[str, Any], decision_policy: Any | None = None
) -> GuidelineDefinition:
    """Validate rules + selectors + decision policy together. Errors name the rule or selector at fault."""
    problems: list[dict[str, str]] = []
    parsed_rules: list[Rule] = []
    for i, raw in enumerate(rules or []):
        try:
            parsed_rules.append(parse_rule(raw))
        except RuleValidationError as exc:
            label = raw.get("rule_id", i) if isinstance(raw, dict) else i
            problems.extend(
                {"field": f"rules.{label}.{e['field']}", "reason": e["reason"]} for e in exc.errors
            )
    parsed_selectors: dict[str, Selector] = {}
    for name, raw in (selectors or {}).items():
        if not re.match(_NAME_RE, str(name)):
            problems.append({"field": f"selectors.{name}", "reason": "invalid selector name"})
            continue
        try:
            parsed_selectors[name] = parse_selector(raw)
        except RuleValidationError as exc:
            problems.extend(
                {"field": f"selectors.{name}.{e['field']}", "reason": e["reason"]} for e in exc.errors
            )
    policy = None
    try:
        policy = _parse(
            DecisionPolicy,
            decision_policy if decision_policy is not None else DEFAULT_DECISION_POLICY,
            "decision policy",
        )
    except RuleValidationError as exc:
        problems.extend({"field": f"decision_policy.{e['field']}", "reason": e["reason"]} for e in exc.errors)
    if problems:
        raise RuleValidationError(problems)
    try:
        return GuidelineDefinition(rules=parsed_rules, selectors=parsed_selectors, decision_policy=policy)
    except ValidationError as exc:
        raise RuleValidationError(_errors(exc)) from exc


def canonical_json(data: Any) -> bytes:
    return orjson.dumps(data, option=orjson.OPT_SORT_KEYS)


def canonical_hash(data: Any) -> str:
    return hashlib.sha256(canonical_json(data)).hexdigest()


KEY_RE = re.compile(r"^[a-z][a-z0-9_\-]{1,79}$")  # guideline_key
