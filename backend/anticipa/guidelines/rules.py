"""Declarative rule format `anticipa-rule-v1` (Docs/08 §6): constrained JSON, pydantic-validated, never executed.

A rule decides ONE care activity for ONE resident from deterministic inputs (08 §3). Shape:

    {
      "care_activity": "example_screening",              # snake_case; becomes CareGapCase.care_activity
      "priority": "routine" | "elevated" | "high",        # default routine (case priority classification)
      "missing_data_policy": "unknown" | "not_satisfied" | "review_required",   # default for leaves; default unknown
      "inclusion": <node>,                                # must evaluate to passed for eligibility
      "exclusions": [ {"id": "...", "outcome": "not_eligible" | "already_completed" | "temporarily_excluded"
                                               | "requires_clinical_review", "condition": <node>} ]
    }

`<node>` is one of: `{"operator": "AND"|"OR", "conditions": [<node>, ...]}`, `{"operator": "NOT", "condition": <node>}`
or a leaf. Leaves (every leaf has a unique `id`; unknown keys, fields and operators are rejected):

  resident.age_years   whole years at the evaluation date; operators EQUALS NOT_EQUALS GREATER_THAN
                       GREATER_THAN_OR_EQUAL LESS_THAN LESS_THAN_OR_EQUAL IN NOT_IN; `value` int (list[int] for IN)
  resident.sex         administrative sex; operators EQUALS NOT_EQUALS IN NOT_IN; `value` male|female|other|unknown
  clinical.condition | clinical.procedure | clinical.observation | clinical.immunization | clinical.diagnostic_report
                       presence of normalised clinical records; operators EXISTS NOT_EXISTS; requires `codes`
                       [{system, code}], optional `lookback` {years, months, days} (record date within the window
                       ending on the evaluation date; future-dated records never count), optional `status_in`
  evidence.fact        presence of ACCEPTED evidence facts; operators EXISTS NOT_EXISTS; requires `category` and
                       `subject` ("resident" | "family_member"); optional `fact_types`, `assertion_status`
                       (default ["positive"]), `temporal_status` (default ["current","historical"]), for family
                       members `relationships` / `max_relationship_degree` (1 = parents, siblings, children), and
                       `age_at_onset` {operator, value}

Missing data (08 §3.3, §10.4): a presence leaf that finds nothing applies `missing_data_policy` to the *existence
predicate*: `unknown` (default: no record is not proof of absence), `not_satisfied` (existence is taken as false, so
EXISTS fails and NOT_EXISTS passes — an explicit rule-author opt-in, rejected for family-history facts), or
`review_required` (unknown, and the outcome becomes requires_clinical_review if nothing conclusive decides it).

Date operators of 08 §6.3 (DATE_BEFORE/AFTER/WITHIN) are deliberately not part of v1: temporal conditions are
expressed with `lookback`, and every evaluation uses an explicit evaluation date.
"""

from __future__ import annotations

import hashlib
import re
from functools import cached_property
from typing import Annotated, Any, Literal, Union

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
    model_validator,
)

RULE_LANGUAGE = "anticipa-rule-v1"
INPUT_SCHEMA_VERSION = "1"

MAX_DEPTH = 8
MAX_LEAVES = 64

MissingPolicy = Literal["unknown", "not_satisfied", "review_required"]
ExclusionOutcome = Literal["not_eligible", "already_completed", "temporarily_excluded", "requires_clinical_review"]
Priority = Literal["routine", "elevated", "high"]
Sex = Literal["male", "female", "other", "unknown"]
AssertionStatus = Literal["positive", "negative"]  # uncertain / conditional evidence can never satisfy a criterion
TemporalStatus = Literal["current", "historical", "planned", "unknown"]
EvidenceCategory = Literal[
    "condition", "family_history", "procedure", "observation", "immunization", "diagnostic_finding", "symptom",
    "risk_factor", "medication", "preventive_care",
]  # fmt: skip
Relationship = Literal[
    "mother", "father", "sister", "brother", "daughter", "son", "spouse", "maternal_grandmother",
    "maternal_grandfather", "paternal_grandmother", "paternal_grandfather", "maternal_aunt", "maternal_uncle",
    "paternal_aunt", "paternal_uncle", "cousin", "half_sister", "half_brother", "other_relative",
]  # fmt: skip

# Genetic degree of relationship (a definition, not a clinical criterion). Spouse and other_relative have no degree.
RELATIONSHIP_DEGREE: dict[str, int] = {
    "mother": 1, "father": 1, "sister": 1, "brother": 1, "daughter": 1, "son": 1,
    "maternal_grandmother": 2, "maternal_grandfather": 2, "paternal_grandmother": 2, "paternal_grandfather": 2,
    "maternal_aunt": 2, "maternal_uncle": 2, "paternal_aunt": 2, "paternal_uncle": 2, "half_sister": 2,
    "half_brother": 2, "cousin": 3,
}  # fmt: skip

CLINICAL_FIELDS: dict[str, str] = {  # leaf field -> clinical_records.category
    "clinical.condition": "condition",
    "clinical.procedure": "procedure",
    "clinical.observation": "observation",
    "clinical.immunization": "immunization",
    "clinical.diagnostic_report": "diagnostic_report",
}
NUMERIC_OPS = frozenset(
    {"EQUALS", "NOT_EQUALS", "GREATER_THAN", "GREATER_THAN_OR_EQUAL", "LESS_THAN", "LESS_THAN_OR_EQUAL", "IN", "NOT_IN"}
)
SET_OPS = frozenset({"EQUALS", "NOT_EQUALS", "IN", "NOT_IN"})
PRESENCE_OPS = frozenset({"EXISTS", "NOT_EXISTS"})
LEAF_FIELDS = ("resident.age_years", "resident.sex", *CLINICAL_FIELDS, "evidence.fact")
LEAF_OPERATORS = tuple(sorted(NUMERIC_OPS | PRESENCE_OPS))
RESERVED_IDS = frozenset({"inclusion", "engine.resident_alive"})
_ID_RE = r"^[a-z][a-z0-9_\-]{0,63}$"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Code(_Closed):
    system: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=64)
    display: str | None = Field(None, max_length=200)  # documentation only; never used for matching


class Lookback(_Closed):
    years: int = Field(0, ge=0, le=120)
    months: int = Field(0, ge=0, le=1440)
    days: int = Field(0, ge=0, le=36500)

    @model_validator(mode="after")
    def _positive(self) -> Lookback:
        if self.years + self.months + self.days <= 0:
            raise ValueError("lookback must be longer than zero")
        return self


class OnsetCondition(_Closed):
    operator: Literal[
        "EQUALS", "NOT_EQUALS", "GREATER_THAN", "GREATER_THAN_OR_EQUAL", "LESS_THAN", "LESS_THAN_OR_EQUAL"
    ]
    value: StrictInt = Field(ge=0, le=130)


class Leaf(_Closed):
    id: str = Field(pattern=_ID_RE)
    label: str | None = Field(None, max_length=300)
    field: Literal[
        "resident.age_years", "resident.sex", "clinical.condition", "clinical.procedure", "clinical.observation",
        "clinical.immunization", "clinical.diagnostic_report", "evidence.fact",
    ]  # fmt: skip
    operator: Literal[
        "EQUALS", "NOT_EQUALS", "GREATER_THAN", "GREATER_THAN_OR_EQUAL", "LESS_THAN", "LESS_THAN_OR_EQUAL", "IN",
        "NOT_IN", "EXISTS", "NOT_EXISTS",
    ]  # fmt: skip
    value: StrictInt | StrictStr | list[StrictInt] | list[StrictStr] | None = None
    # presence parameters (clinical.*)
    codes: list[Code] | None = Field(None, min_length=1, max_length=200)
    lookback: Lookback | None = None
    status_in: list[str] | None = Field(None, min_length=1)
    # evidence.fact parameters
    category: EvidenceCategory | None = None
    subject: Literal["resident", "family_member"] | None = None
    fact_types: list[str] | None = Field(None, min_length=1)
    assertion_status: list[AssertionStatus] | None = Field(None, min_length=1)
    temporal_status: list[TemporalStatus] | None = Field(None, min_length=1)
    relationships: list[Relationship] | None = Field(None, min_length=1)
    max_relationship_degree: int | None = Field(None, ge=1, le=3)
    age_at_onset: OnsetCondition | None = None
    missing_data_policy: MissingPolicy | None = None  # None -> the rule's default

    @cached_property
    def codeset(self) -> frozenset[tuple[str, str]]:
        return frozenset((c.system, c.code) for c in self.codes or ())

    @cached_property
    def status_set(self) -> frozenset[str] | None:
        return frozenset(self.status_in) if self.status_in else None

    @model_validator(mode="after")
    def _check_field_contract(self) -> Leaf:
        used = {
            k for k in ("value", "codes", "lookback", "status_in", "category", "subject", "fact_types",
                        "assertion_status", "temporal_status", "relationships", "max_relationship_degree",
                        "age_at_onset")
            if getattr(self, k) is not None
        }  # fmt: skip
        allowed: set[str]
        if self.field == "resident.age_years":
            self._need_operator(NUMERIC_OPS)
            allowed = {"value"}
            self._value_ints()
        elif self.field == "resident.sex":
            self._need_operator(SET_OPS)
            allowed = {"value"}
            self._value_sexes()
        elif self.field in CLINICAL_FIELDS:
            self._need_operator(PRESENCE_OPS)
            allowed = {"codes", "lookback", "status_in"}
            if self.codes is None:
                raise ValueError(f"{self.field} requires `codes`")
        else:  # evidence.fact
            self._need_operator(PRESENCE_OPS)
            allowed = {"category", "subject", "fact_types", "assertion_status", "temporal_status", "age_at_onset"}
            if self.category is None or self.subject is None:
                raise ValueError("evidence.fact requires `category` and `subject`")
            if self.subject == "family_member":
                allowed |= {"relationships", "max_relationship_degree"}
                if self.missing_data_policy == "not_satisfied":
                    raise ValueError(
                        "missing_data_policy `not_satisfied` is not allowed for family history: "
                        "a missing family-history entry never proves the absence of family history"
                    )
        extra = used - allowed
        if extra:
            raise ValueError(f"parameters {sorted(extra)} are not valid for field {self.field}")
        return self

    def _need_operator(self, ops: frozenset[str]) -> None:
        if self.operator not in ops:
            raise ValueError(f"operator {self.operator} is not valid for field {self.field}; use one of {sorted(ops)}")

    def _value_ints(self) -> None:
        v = self.value
        if self.operator in ("IN", "NOT_IN"):
            if not isinstance(v, list) or not v or not all(isinstance(x, int) for x in v):
                raise ValueError(f"{self.operator} requires a non-empty list of integers")
        elif not isinstance(v, int):
            raise ValueError(f"{self.operator} requires an integer `value`")

    def _value_sexes(self) -> None:
        v = self.value
        items = v if isinstance(v, list) else [v]
        if (self.operator in ("IN", "NOT_IN")) != isinstance(v, list):
            raise ValueError(f"{self.operator} {'requires a list' if isinstance(v, str) else 'requires a single value'}")
        if not items or not all(isinstance(x, str) and x in ("male", "female", "other", "unknown") for x in items):
            raise ValueError("sex values must be male, female, other or unknown")


class AndOrGroup(_Closed):
    id: str | None = Field(None, pattern=_ID_RE)
    label: str | None = Field(None, max_length=300)
    operator: Literal["AND", "OR"]
    conditions: list[Node] = Field(min_length=1)


class NotGroup(_Closed):
    id: str | None = Field(None, pattern=_ID_RE)
    label: str | None = Field(None, max_length=300)
    operator: Literal["NOT"]
    condition: Node


def _node_tag(value: Any) -> str:
    op = value.get("operator") if isinstance(value, dict) else getattr(value, "operator", None)
    if op in ("AND", "OR"):
        return "group"
    if op == "NOT":
        return "not"
    return "leaf"


Node = Annotated[
    Union[Annotated[AndOrGroup, Tag("group")], Annotated[NotGroup, Tag("not")], Annotated[Leaf, Tag("leaf")]],  # noqa: UP007
    Discriminator(_node_tag),
]


class Exclusion(_Closed):
    id: str = Field(pattern=_ID_RE)
    label: str | None = Field(None, max_length=300)
    outcome: ExclusionOutcome = "not_eligible"
    condition: Node


class RuleDefinition(_Closed):
    care_activity: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    priority: Priority = "routine"
    missing_data_policy: MissingPolicy = "unknown"
    inclusion: Node
    exclusions: list[Exclusion] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def _structure(self) -> RuleDefinition:
        ids: list[str] = [e.id for e in self.exclusions]
        leaves = 0

        def walk(node: Node, depth: int) -> None:
            nonlocal leaves
            if depth > MAX_DEPTH:
                raise ValueError(f"expression nesting is deeper than {MAX_DEPTH}")
            if isinstance(node, Leaf):
                leaves += 1
                ids.append(node.id)
            elif isinstance(node, NotGroup):
                walk(node.condition, depth + 1)
            else:
                for child in node.conditions:
                    walk(child, depth + 1)

        walk(self.inclusion, 1)
        for e in self.exclusions:
            walk(e.condition, 1)
        if leaves > MAX_LEAVES:
            raise ValueError(f"a rule may have at most {MAX_LEAVES} criteria")
        dupes = sorted({i for i in ids if ids.count(i) > 1} | (set(ids) & RESERVED_IDS))
        if dupes:
            raise ValueError(f"criterion ids must be unique and not reserved: {dupes}")
        return self

    # -- canonical form ---------------------------------------------------------------------------------------------
    def normalized(self) -> dict[str, Any]:
        """The canonical stored form (what the author wrote plus top-level defaults; unset optionals omitted)."""
        return self.model_dump(mode="json", exclude_none=True)

    def content_hash(self) -> str:
        return canonical_hash(self.normalized())

    def leaves(self) -> list[Leaf]:
        out: list[Leaf] = []

        def walk(node: Node) -> None:
            if isinstance(node, Leaf):
                out.append(node)
            elif isinstance(node, NotGroup):
                walk(node.condition)
            else:
                for child in node.conditions:
                    walk(child)

        walk(self.inclusion)
        for e in self.exclusions:
            walk(e.condition)
        return out


AndOrGroup.model_rebuild()
NotGroup.model_rebuild()
Exclusion.model_rebuild()
RuleDefinition.model_rebuild()


class RuleValidationError(ValueError):
    """Raised by `parse_rule_definition`; `.errors` is a list of {"field", "reason"} dicts safe to return to clients."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("; ".join(f"{e['field']}: {e['reason']}" for e in errors))
        self.errors = errors


def _json_depth(value: Any, limit: int) -> bool:
    """True when JSON nesting exceeds `limit` (iterative: never trips the recursion limit)."""
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


def parse_rule_definition(raw: Any) -> RuleDefinition:
    """Validate untrusted JSON into a RuleDefinition (the only way rule JSON enters the engine)."""
    if not isinstance(raw, dict):
        raise RuleValidationError([{"field": "$", "reason": "a rule definition must be a JSON object"}])
    if _json_depth(raw, MAX_DEPTH * 3 + 6):
        raise RuleValidationError([{"field": "$", "reason": "rule definition is nested too deeply"}])
    try:
        return RuleDefinition.model_validate(raw)
    except ValidationError as exc:
        raise RuleValidationError(
            [
                {"field": ".".join(str(p) for p in err["loc"]) or "$", "reason": err["msg"]}
                for err in exc.errors(include_url=False, include_input=False)
            ][:50]
        ) from exc


def canonical_json(data: Any) -> bytes:
    return orjson.dumps(data, option=orjson.OPT_SORT_KEYS)


def canonical_hash(data: Any) -> str:
    return hashlib.sha256(canonical_json(data)).hexdigest()


KEY_RE = re.compile(r"^[a-z][a-z0-9_\-]{1,79}$")  # guideline_key / rule_key
