"""Pure deterministic rule evaluator (Docs/08 §7-§10). No I/O, no clock, no randomness, no LLM.

    evaluate_rule(rule, inputs, evaluation_date) -> RuleResult

is a pure function of the validated rule, the normalised inputs and the explicit evaluation date, so the same inputs
always give byte-identical results (08 §8.2). Criteria use three-valued logic (passed / failed / unknown); missing
information is never silently turned into a negative clinical finding (08 §3.3).

Outcomes (04 §11.2) and their 08 class:
    eligible                    -> eligible
    not_eligible, already_completed, temporarily_excluded            -> ineligible
    insufficient_evidence, requires_clinical_review                   -> indeterminate
    evaluation_error                                                  -> error

Evaluation order: engine prerequisite (resident alive) -> inclusion -> exclusions (skipped as not_applicable when the
inclusion failed). A definitively applying exclusion wins over an unknown inclusion; unknown anything else is
indeterminate. Only evidence facts with validation_status `accepted` and subject_type matching the leaf are ever used,
so facts about relatives are never attributed to the resident (and the reverse).
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from anticipa.guidelines.rules import (
    CLINICAL_FIELDS,
    INPUT_SCHEMA_VERSION,
    RELATIONSHIP_DEGREE,
    AndOrGroup,
    Leaf,
    NotGroup,
    RuleDefinition,
    canonical_hash,
)

ENGINE_VERSION = "guideline-engine/1.0.0"

OUTCOMES = (
    "eligible", "not_eligible", "already_completed", "insufficient_evidence", "requires_clinical_review",
    "temporarily_excluded", "evaluation_error",
)  # fmt: skip
OUTCOME_CLASSES = ("eligible", "ineligible", "indeterminate", "error")
OUTCOME_CLASS: dict[str, str] = {
    "eligible": "eligible",
    "not_eligible": "ineligible",
    "already_completed": "ineligible",
    "temporarily_excluded": "ineligible",
    "insufficient_evidence": "indeterminate",
    "requires_clinical_review": "indeterminate",
    "evaluation_error": "error",
}
CRITERION_OUTCOMES = ("passed", "failed", "unknown", "not_applicable", "error")
# precedence among exclusions that definitively apply (first wins); requires_clinical_review is indeterminate-class
_EXCLUSION_PRECEDENCE = ("not_eligible", "already_completed", "temporarily_excluded", "requires_clinical_review")
# FHIR statuses that never evidence that something happened
DEFAULT_IGNORED_STATUSES = frozenset({"entered-in-error", "not-done", "cancelled"})
DEFAULT_ASSERTIONS = ("positive",)
DEFAULT_TEMPORAL = ("current", "historical")


# ---------------------------------------------------------------- inputs (normalised, immutable)
@dataclass(frozen=True, slots=True)
class ResidentFacts:
    id: uuid.UUID
    birth_date: date | None
    deceased_date: date | None
    sex: str | None


@dataclass(frozen=True, slots=True)
class RecordFact:
    """A normalised `clinical_records` row (structured evidence)."""

    id: uuid.UUID
    category: str
    code_system: str | None
    code: str | None
    normalized_code_system: str | None
    normalized_code: str | None
    start: date | None  # UTC calendar date of effective_start
    status: str | None
    source_reference: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """An `evidence_facts` row. `validation_status` is checked here too: only `accepted` is ever used."""

    id: uuid.UUID
    category: str
    fact_type: str
    subject_type: str
    relationship: str | None
    assertion: str
    temporal: str
    age_at_onset: int | None
    validation_status: str = "accepted"


@dataclass(frozen=True, slots=True)
class ResidentInputs:
    resident: ResidentFacts
    records: Sequence[RecordFact] = ()
    facts: Sequence[EvidenceItem] = ()


# ---------------------------------------------------------------- results
@dataclass(slots=True)
class Res:
    outcome: str  # passed | failed | unknown
    text: str
    refs: list[dict[str, str]] = field(default_factory=list)
    review: bool = False  # only meaningful when outcome == unknown


@dataclass(slots=True)
class CriterionResult:
    criterion_id: str
    scope: str  # engine | inclusion | exclusion:<id>
    label: str | None
    outcome: str
    explanation: str
    evidence_refs: list[dict[str, str]] = field(default_factory=list)
    review_required: bool = False
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "criterion_id": self.criterion_id, "scope": self.scope, "label": self.label, "field": self.field,
            "outcome": self.outcome, "explanation": self.explanation, "evidence_refs": self.evidence_refs,
        }  # fmt: skip
        if self.review_required:
            d["review_required"] = True
        return d


@dataclass(slots=True)
class RuleResult:
    outcome: str
    basis: str
    criteria: list[CriterionResult]
    evidence_references: list[dict[str, str]]
    missing_information: list[dict[str, Any]]
    exclusion_reasons: list[dict[str, Any]]

    @property
    def outcome_class(self) -> str:
        return OUTCOME_CLASS[self.outcome]

    def explanation(self, rule_key: str | None, evaluation_date: date) -> dict[str, Any]:
        return {
            "engine_version": ENGINE_VERSION,
            "rule_key": rule_key,
            "evaluation_date": evaluation_date.isoformat(),
            "decision": {"outcome": self.outcome, "outcome_class": self.outcome_class, "basis": self.basis},
            "criteria": [c.as_dict() for c in self.criteria],
        }


# ---------------------------------------------------------------- date helpers (explicit evaluation date only)
def age_years(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def shift_back(on: date, years: int, months: int, days: int) -> date:
    """`on` minus years/months/days; the day is clamped to the target month's length (calendar arithmetic)."""
    y, m0 = divmod(on.year * 12 + (on.month - 1) - years * 12 - months, 12)
    month_start = date(y, m0 + 1, min(on.day, calendar.monthrange(y, m0 + 1)[1]))
    return month_start - timedelta(days=days)


# ---------------------------------------------------------------- leaf evaluation
def _compare(op: str, left: int, right: Any) -> bool:
    if op == "EQUALS":
        return left == right
    if op == "NOT_EQUALS":
        return left != right
    if op == "GREATER_THAN":
        return left > right
    if op == "GREATER_THAN_OR_EQUAL":
        return left >= right
    if op == "LESS_THAN":
        return left < right
    if op == "LESS_THAN_OR_EQUAL":
        return left <= right
    if op == "IN":
        return left in right
    if op == "NOT_IN":
        return left not in right
    raise ValueError(f"unsupported operator {op}")  # unreachable for validated rules


def _policy_missing(policy: str, what: str, presence_op: str | None = None) -> Res:
    """Apply a missing-data policy. For presence leaves the policy is about the existence predicate."""
    if policy == "not_satisfied":
        if presence_op is None:
            return Res("failed", f"{what}; treated as not satisfied by the rule's missing-data policy.")
        passed = presence_op == "NOT_EXISTS"
        return Res(
            "passed" if passed else "failed",
            f"{what}; existence is treated as false by the rule's missing-data policy.",
        )
    return Res("unknown", f"{what}.", review=policy == "review_required")


def _eval_age(leaf: Leaf, policy: str, inputs: ResidentInputs, on: date) -> Res:
    birth = inputs.resident.birth_date
    if birth is None:
        return _policy_missing(policy, "The date of birth is not available")
    if birth > on:
        raise ValueError("date of birth is after the evaluation date")
    age = age_years(birth, on)
    ref = [{"type": "resident_attribute", "attribute": "birth_date"}]
    ok = _compare(leaf.operator, age, leaf.value)
    return Res(
        "passed" if ok else "failed",
        f"Age is {age} on {on.isoformat()}; condition {leaf.operator} {leaf.value} is {'met' if ok else 'not met'}.",
        ref,
    )


def _eval_sex(leaf: Leaf, policy: str, inputs: ResidentInputs) -> Res:
    sex = inputs.resident.sex
    if sex is None or sex == "unknown":
        return _policy_missing(policy, "The administrative sex is not recorded")
    value = leaf.value
    ok = (sex in value) if leaf.operator in ("IN", "NOT_IN") and isinstance(value, list) else (sex == value)
    if leaf.operator in ("NOT_EQUALS", "NOT_IN"):
        ok = not ok
    ref = [{"type": "resident_attribute", "attribute": "administrative_sex"}]
    return Res("passed" if ok else "failed", f"Administrative sex is {sex}; condition {leaf.operator} is {'met' if ok else 'not met'}.", ref)


def _record_matches(rec: RecordFact, category: str, codeset: frozenset[tuple[str, str]], ignored: frozenset[str],
                    status_in: frozenset[str] | None) -> bool:  # fmt: skip
    if rec.category != category:
        return False
    if not (
        (rec.code_system and rec.code and (rec.code_system, rec.code) in codeset)
        or (rec.normalized_code_system and rec.normalized_code and (rec.normalized_code_system, rec.normalized_code) in codeset)
    ):
        return False
    if status_in is not None:
        return rec.status in status_in
    return rec.status not in ignored


def _eval_clinical(leaf: Leaf, policy: str, inputs: ResidentInputs, on: date) -> Res:
    category = CLINICAL_FIELDS[leaf.field]
    codeset = leaf.codeset
    status_in = leaf.status_set
    window_start = shift_back(on, leaf.lookback.years, leaf.lookback.months, leaf.lookback.days) if leaf.lookback else None
    found: list[RecordFact] = []
    undated = 0
    for rec in inputs.records:
        if not _record_matches(rec, category, codeset, DEFAULT_IGNORED_STATUSES, status_in):
            continue
        if rec.start is None:
            undated += 1
            if window_start is None:
                found.append(rec)
            continue
        if rec.start > on or (window_start is not None and rec.start < window_start):
            continue  # future-dated (not yet happened) or outside the lookback window
        found.append(rec)
    window = f" within the last {_lookback_text(leaf)}" if leaf.lookback else ""
    if found:
        refs = sorted(
            ({"type": "clinical_record", "id": str(r.id), "source_reference": r.source_reference or ""} for r in found),
            key=lambda d: d["id"],
        )
        exists = leaf.operator == "EXISTS"
        return Res("passed" if exists else "failed", f"{len(found)} matching {category} record(s) found{window}.", refs)
    if undated and window_start is not None:
        return Res("unknown", f"A matching {category} record has no date, so the lookback cannot be applied.")
    return _policy_missing(policy, f"No matching {category} record found{window}", leaf.operator)


def _lookback_text(leaf: Leaf) -> str:
    lb = leaf.lookback
    parts = [f"{n} {u}" for n, u in ((lb.years, "year(s)"), (lb.months, "month(s)"), (lb.days, "day(s)")) if n]
    return " ".join(parts)


def _eval_evidence(leaf: Leaf, policy: str, inputs: ResidentInputs) -> Res:
    wanted_assertions = frozenset(leaf.assertion_status or DEFAULT_ASSERTIONS)
    wanted_temporal = frozenset(leaf.temporal_status or DEFAULT_TEMPORAL)
    fact_types = frozenset(leaf.fact_types) if leaf.fact_types else None
    relationships = frozenset(leaf.relationships) if leaf.relationships else None
    base: list[EvidenceItem] = []  # accepted facts about the right subject/category/type/relationship
    for f in inputs.facts:
        if f.validation_status != "accepted" or f.category != leaf.category or f.subject_type != leaf.subject:
            continue
        if fact_types is not None and f.fact_type not in fact_types:
            continue
        if leaf.subject == "family_member":
            if f.relationship is None:
                continue
            if relationships is not None and f.relationship not in relationships:
                continue
            if leaf.max_relationship_degree is not None:
                degree = RELATIONSHIP_DEGREE.get(f.relationship)
                if degree is None or degree > leaf.max_relationship_degree:
                    continue
        base.append(f)

    asserted = {f.assertion for f in base}
    if "positive" in asserted and "negative" in asserted:  # 08 §10.3: never pick a side
        refs = _fact_refs(f for f in base if f.assertion in ("positive", "negative"))
        return Res("unknown", "Accepted evidence contradicts itself (positive and negative statements); review required.", refs, review=True)

    matching = [f for f in base if f.assertion in wanted_assertions and f.temporal in wanted_temporal]
    satisfied: list[EvidenceItem] = []
    onset_unknown: list[EvidenceItem] = []
    for f in matching:
        if leaf.age_at_onset is None:
            satisfied.append(f)
        elif f.age_at_onset is None:
            onset_unknown.append(f)
        elif _compare(leaf.age_at_onset.operator, f.age_at_onset, leaf.age_at_onset.value):
            satisfied.append(f)
    if satisfied:
        exists = leaf.operator == "EXISTS"
        return Res("passed" if exists else "failed", f"{len(satisfied)} accepted evidence fact(s) match.", _fact_refs(satisfied))
    if onset_unknown:
        return Res(
            "unknown",
            "A matching evidence fact exists but its age at onset is not documented.",
            _fact_refs(onset_unknown),
        )
    unsure = [f for f in base if f.assertion in ("uncertain", "conditional")]
    if unsure:  # uncertain evidence never becomes certain, and never counts as absence either
        return Res("unknown", "Only uncertain or conditional evidence exists; clarification required.", _fact_refs(unsure), review=True)
    return _policy_missing(policy, "No accepted evidence fact matches", leaf.operator)


def _fact_refs(facts: Any) -> list[dict[str, str]]:
    return sorted(({"type": "evidence_fact", "id": str(f.id)} for f in facts), key=lambda d: d["id"])


def _eval_leaf(leaf: Leaf, default_policy: str, inputs: ResidentInputs, on: date) -> Res:
    policy = leaf.missing_data_policy or default_policy
    if leaf.field == "resident.age_years":
        return _eval_age(leaf, policy, inputs, on)
    if leaf.field == "resident.sex":
        return _eval_sex(leaf, policy, inputs)
    if leaf.field in CLINICAL_FIELDS:
        return _eval_clinical(leaf, policy, inputs, on)
    return _eval_evidence(leaf, policy, inputs)


# ---------------------------------------------------------------- expression tree (Kleene logic)
def _combine_and(results: list[Res]) -> Res:
    failed = [r for r in results if r.outcome == "failed"]
    if failed:
        return Res("failed", "At least one required condition failed.", _merge_refs(failed))
    unknown = [r for r in results if r.outcome == "unknown"]
    if unknown:
        return Res("unknown", "No condition failed but at least one is unknown.", _merge_refs(unknown), any(r.review for r in unknown))
    return Res("passed", "All conditions passed.", _merge_refs(results))


def _combine_or(results: list[Res]) -> Res:
    passed = [r for r in results if r.outcome == "passed"]
    if passed:
        return Res("passed", "At least one alternative passed.", _merge_refs(passed))
    unknown = [r for r in results if r.outcome == "unknown"]
    if unknown:
        return Res("unknown", "No alternative passed but at least one is unknown.", _merge_refs(unknown), any(r.review for r in unknown))
    return Res("failed", "No alternative passed.", _merge_refs(results))


def _merge_refs(results: list[Res]) -> list[dict[str, str]]:
    seen: dict[tuple[str, str], dict[str, str]] = {}
    for r in results:
        for ref in r.refs:
            seen[(ref["type"], ref.get("id") or ref.get("attribute", ""))] = ref
    return [seen[k] for k in sorted(seen)]


class _Walker:
    def __init__(self, default_policy: str, inputs: ResidentInputs, on: date, scope: str, sink: list[CriterionResult]):
        self.default_policy, self.inputs, self.on, self.scope, self.sink = default_policy, inputs, on, scope, sink

    def node(self, node: Any) -> Res:
        if isinstance(node, Leaf):
            res = _eval_leaf(node, self.default_policy, self.inputs, self.on)
            self.sink.append(
                CriterionResult(node.id, self.scope, node.label, res.outcome, res.text, res.refs,
                                res.review and res.outcome == "unknown", node.field)  # fmt: skip
            )
            return res
        if isinstance(node, NotGroup):
            inner = self.node(node.condition)
            flipped = {"passed": "failed", "failed": "passed", "unknown": "unknown"}[inner.outcome]
            return Res(flipped, f"NOT of: {inner.text}", inner.refs, inner.review)
        results = [self.node(child) for child in node.conditions]
        return _combine_and(results) if isinstance(node, AndOrGroup) and node.operator == "AND" else _combine_or(results)


# ---------------------------------------------------------------- the evaluation
def evaluate_rule(rule: RuleDefinition, inputs: ResidentInputs, evaluation_date: date) -> RuleResult:
    """Evaluate one rule for one resident on `evaluation_date`. Never raises: failures become `evaluation_error`."""
    criteria: list[CriterionResult] = []
    try:
        return _evaluate(rule, inputs, evaluation_date, criteria)
    except Exception as exc:  # noqa: BLE001 - a rule execution error is a recorded outcome (08 §12), not a crash
        criteria.append(
            CriterionResult("engine.error", "engine", None, "error", f"{type(exc).__name__}: {str(exc)[:200]}")
        )
        return RuleResult("evaluation_error", "evaluation_error", criteria, [], [], [])


def _evaluate(rule: RuleDefinition, inputs: ResidentInputs, on: date, criteria: list[CriterionResult]) -> RuleResult:
    resident = inputs.resident
    if resident.deceased_date is not None and resident.deceased_date <= on:
        criteria.append(
            CriterionResult("engine.resident_alive", "engine", "Resident is alive on the evaluation date", "failed",
                            f"Deceased on {resident.deceased_date.isoformat()}, on or before {on.isoformat()}.",
                            [{"type": "resident_attribute", "attribute": "deceased_date"}])  # fmt: skip
        )
        for e in rule.exclusions:
            criteria.append(_skipped(e.id, f"exclusion:{e.id}", e.label))
        return RuleResult("not_eligible", "resident_deceased", criteria, _refs_of(criteria), [], [])

    walker = _Walker(rule.missing_data_policy, inputs, on, "inclusion", criteria)
    inclusion = walker.node(rule.inclusion)
    criteria.append(CriterionResult("inclusion", "inclusion", "Inclusion criteria", inclusion.outcome, inclusion.text, inclusion.refs, inclusion.review and inclusion.outcome == "unknown"))  # fmt: skip

    excl_results: list[tuple[Any, Res]] = []
    if inclusion.outcome == "failed":
        for e in rule.exclusions:
            criteria.append(_skipped(e.id, f"exclusion:{e.id}", e.label))
        return RuleResult("not_eligible", "inclusion_not_met", criteria, _refs_of(criteria), [], [])

    for e in rule.exclusions:
        res = _Walker(rule.missing_data_policy, inputs, on, f"exclusion:{e.id}", criteria).node(e.condition)
        criteria.append(CriterionResult(e.id, f"exclusion:{e.id}", e.label, res.outcome, res.text, res.refs, res.review and res.outcome == "unknown"))  # fmt: skip
        excl_results.append((e, res))

    applied = [(e, r) for e, r in excl_results if r.outcome == "passed"]
    exclusion_reasons = [
        {"exclusion_id": e.id, "outcome": e.outcome, "label": e.label, "evidence_refs": r.refs} for e, r in applied
    ]
    unknown_excl = [(e, r) for e, r in excl_results if r.outcome == "unknown"]
    outcome, basis = _decide(inclusion, applied, unknown_excl)
    # ponytail: lists every unknown leaf of an indeterminate evaluation, including ones inside an already-decided OR
    # branch; narrow to "required" unknowns if reviewers find that noisy.
    group_ids = {"inclusion", *(e.id for e in rule.exclusions)}
    missing = (
        [
            {"criterion_id": c.criterion_id, "scope": c.scope, "field": c.field, "reason": c.explanation}
            for c in criteria
            if c.outcome == "unknown" and c.criterion_id not in group_ids
        ]
        if OUTCOME_CLASS[outcome] == "indeterminate"
        else []
    )
    return RuleResult(outcome, basis, criteria, _refs_of(criteria), missing, exclusion_reasons)


def _decide(inclusion: Res, applied: list[tuple[Any, Res]], unknown_excl: list[tuple[Any, Res]]) -> tuple[str, str]:
    applied_outcomes = {e.outcome for e, _ in applied}
    for outcome in _EXCLUSION_PRECEDENCE[:-1]:  # definitive ineligible-class exclusions win, even over unknown inclusion
        if outcome in applied_outcomes:
            return outcome, "exclusion_applied"
    if "requires_clinical_review" in applied_outcomes:
        return "requires_clinical_review", "exclusion_requires_review"
    if inclusion.outcome == "unknown" or unknown_excl:
        review = (inclusion.outcome == "unknown" and inclusion.review) or any(r.review for _, r in unknown_excl)
        return ("requires_clinical_review" if review else "insufficient_evidence"), "missing_information"
    return "eligible", "all_criteria_met"


def _skipped(cid: str, scope: str, label: str | None) -> CriterionResult:
    return CriterionResult(cid, scope, label, "not_applicable", "Not evaluated because the inclusion criteria were not met.")


def _refs_of(criteria: list[CriterionResult]) -> list[dict[str, str]]:
    return _merge_refs([Res("", "", c.evidence_refs) for c in criteria if c.outcome in ("passed", "failed", "unknown")])


# ---------------------------------------------------------------- input snapshot / hash (incremental evaluation)
@dataclass(frozen=True, slots=True)
class RuleDeps:
    """The inputs a rule can depend on (08 §15.2); computed once per rule version, reused for every resident."""

    uses_birth_date: bool
    uses_sex: bool
    clinical_categories: frozenset[str]
    codes: frozenset[tuple[str, str]]
    evidence_categories: frozenset[str]

    @classmethod
    def of(cls, rule: RuleDefinition) -> RuleDeps:
        leaves = rule.leaves()
        return cls(
            any(lf.field == "resident.age_years" for lf in leaves),
            any(lf.field == "resident.sex" for lf in leaves),
            frozenset(CLINICAL_FIELDS[lf.field] for lf in leaves if lf.field in CLINICAL_FIELDS),
            frozenset(c for lf in leaves if lf.field in CLINICAL_FIELDS for c in lf.codeset),
            frozenset(lf.category for lf in leaves if lf.field == "evidence.fact" and lf.category),
        )


def input_snapshot(deps: RuleDeps, rule_hash: str, inputs: ResidentInputs, evaluation_date: date) -> dict[str, Any]:
    """Everything the evaluation can depend on, in canonical form. Hashing this gives the incremental-skip key."""
    r = inputs.resident
    codes = deps.codes
    records = sorted(
        (
            [str(x.id), x.category, x.code_system, x.code, x.normalized_code_system, x.normalized_code,
             x.start.isoformat() if x.start else None, x.status]
            for x in inputs.records
            if x.category in deps.clinical_categories
            and ((x.code_system, x.code) in codes or (x.normalized_code_system, x.normalized_code) in codes)
        ),
        key=lambda row: row[0],
    )  # fmt: skip
    facts = sorted(
        (
            [str(f.id), f.category, f.fact_type, f.subject_type, f.relationship, f.assertion, f.temporal,
             f.age_at_onset, f.validation_status]
            for f in inputs.facts
            if f.category in deps.evidence_categories and f.validation_status == "accepted"
        ),
        key=lambda row: row[0],
    )  # fmt: skip
    return {
        "engine": ENGINE_VERSION,
        "input_schema": INPUT_SCHEMA_VERSION,
        "rule_hash": rule_hash,
        "evaluation_date": evaluation_date.isoformat(),
        "resident": {
            "id": str(r.id),
            "birth_date": r.birth_date.isoformat() if deps.uses_birth_date and r.birth_date else None,
            "sex": r.sex if deps.uses_sex else None,
            "deceased_date": r.deceased_date.isoformat() if r.deceased_date else None,
        },
        "records": records,
        "facts": facts,
    }


def input_hash(deps: RuleDeps, rule_hash: str, inputs: ResidentInputs, evaluation_date: date) -> str:
    return canonical_hash(input_snapshot(deps, rule_hash, inputs, evaluation_date))
