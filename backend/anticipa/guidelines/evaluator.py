"""Pure deterministic evaluator for 08-format guideline versions (Docs/08 §7-§10, rules schema v2). No I/O, no
clock, no
randomness, no LLM.

    evaluate_guideline(definition, inputs, evaluation_date, sources) -> GuidelineResult

is a pure function of the validated guideline definition, the normalised inputs and the explicit evaluation
date, so
identical inputs always give identical results (08 §8.2). Expressions use Kleene three-valued logic (true /
false /
unknown). Each rule maps its expression result to a criterion outcome (`criterion_semantics`) and to a
contribution to
the overall outcome (`outcome_by_expression_result`); the decision policy ranks contributions and the
best-ranked one wins
(none -> eligible). Missing information is never silently turned into a negative clinical finding: only where
a rule's own semantics say so (D-01, "no documented X") let absence change an outcome.

Evidence handling (07, 08 §10): only facts with validation_status `accepted` are definite; uncertain /
conditional / not
yet accepted facts and relatives of undecidable relevance are UNRESOLVED candidates; aggregates over subjects
are
intervals [definite, definite + unresolved] so the engine never claims more certainty than the evidence gives;
positive
and negative facts about one subject, or two different ages at onset for one relative, are reported as
conflicts and
never resolved silently.
"""

from __future__ import annotations

import calendar
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from anticipa.guidelines.rules import (
    AGE_FIELD,
    INPUT_SCHEMA_VERSION,
    OUTCOME_CLASS,
    SEX_FIELD,
    AndOrGroup,
    Duration,
    FieldRef,
    GuidelineDefinition,
    Leaf,
    NotGroup,
    Rule,
    Selector,
    canonical_hash,
    leaves_of,
    parse_field,
)

ENGINE_VERSION = "guideline-engine/2.1.0"
REVIEW_PENDING = "pending_clinical_review"
REVIEW_DONE = "clinically_reviewed"
CLINICAL_REVIEW_STATUSES = (REVIEW_PENDING, REVIEW_DONE)

SYSTEM_ALIASES = {"http://snomed.info/sct": "SNOMED-CT", "http://loinc.org": "LOINC"}
# structured clinical_records.category -> evidence category used by selectors
RECORD_CATEGORY = {
    "condition": "condition",
    "procedure": "procedure",
    "observation": "observation",
    "immunization": "immunization",
    "diagnostic_report": "diagnostic_finding",
    "medication": "medication",
}
IGNORED_RECORD_STATUSES = frozenset({"entered-in-error", "not-done", "cancelled"})
PLANNED_RECORD_STATUSES = frozenset({"preparation", "planned", "scheduled", "proposed"})
DEFAULT_COVERAGE_YEARS = 5
INF = math.inf


# ---------------------------------------------------------------- inputs (normalised, immutable)
@dataclass(frozen=True, slots=True)
class ResidentFacts:
    id: uuid.UUID
    birth_date: date | None
    deceased_date: date | None
    sex: (
        str | None
    )  # sex assigned at birth (from residents.administrative_sex for Synthea data, assumption A-02)


@dataclass(frozen=True, slots=True)
class Fact:
    """One piece of evidence: a structured clinical record (source 'record') or an evidence fact ('fact')."""

    id: str
    source: str  # record | fact
    category: str
    fact_type: str | None
    code_system: str | None
    code: str | None
    subject_type: str
    relationship: str | None
    subject_key: str  # identifies the person the fact is about (distinct relatives are counted by this)
    assertion: str
    temporal: str
    validation: str
    date_start: date | None = None  # partial dates carry their full range (08 §9.1)
    date_end: date | None = None
    onset: int | None = None
    doc_id: str | None = None
    quote: str | None = None
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class ResidentInputs:
    resident: ResidentFacts
    facts: Sequence[Fact] = ()
    latest_record_date: date | None = None  # most recent structured clinical record (coverage prerequisite)


def record_to_fact(
    rid: uuid.UUID | str,
    category: str,
    system: str | None,
    code: str | None,
    start: date | None,
    end: date | None,
    status: str | None,
    on: date,
    reference: str | None = None,
) -> Fact | None:
    """Normalise a clinical_records row; None for records that evidence nothing (entered in error, not
    done...)."""  # noqa: E501
    if status in IGNORED_RECORD_STATUSES:
        return None
    if start is not None and start > on or status in PLANNED_RECORD_STATUSES:
        temporal = "planned"
    elif category == "condition" and end is not None and end <= on:
        temporal = "historical"
    else:
        temporal = "current" if category == "condition" else "historical"
    return Fact(
        str(rid),
        "record",
        RECORD_CATEGORY.get(category, category),
        None,
        SYSTEM_ALIASES.get(system or "", system),
        code,
        "resident",
        None,
        "self",
        "positive",
        temporal,
        "accepted",
        start,
        start,
        None,
        None,
        None,
        reference,
    )


# ---------------------------------------------------------------- results
@dataclass(slots=True)
class Tri:
    value: bool | None
    text: str = ""
    refs: list[dict[str, Any]] = field(default_factory=list)
    review: bool = False  # an unknown that a person should look at
    conflicts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class CriterionResult:
    rule_id: str
    rule_type: str
    priority: int
    result: bool | None
    outcome: str  # criterion outcome (08 §7.1)
    contribution: str | None  # contribution to the overall outcome (or None)
    review_class: str | None
    detail: str
    reason_code: str
    refs: list[dict[str, Any]]
    review: bool = False

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "criterion_id": self.rule_id,
            "rule_type": self.rule_type,
            "priority": self.priority,
            "result": "unknown" if self.result is None else self.result,
            "outcome": self.outcome,
            "detail": self.detail,
        }
        if self.contribution:
            d["contributes"] = self.contribution
        if self.outcome != "passed":
            d["reason_code"] = self.reason_code
        if self.review:
            d["review_required"] = True
        return d


@dataclass(slots=True)
class GuidelineResult:
    outcome: str  # one of the seven 04 §11.2 outcomes
    outcome_class: str  # eligible | ineligible | indeterminate | error
    reason_code: str | None
    decisive_rule_id: str | None
    basis: str
    explanation_text: str | None
    criteria: list[CriterionResult]
    evidence_references: list[dict[str, Any]]
    missing_information: list[dict[str, Any]]
    conflicting_information: list[dict[str, Any]]
    exclusion_reasons: list[dict[str, Any]]
    source_references: list[dict[str, Any]]

    def explanation(self, guideline: dict[str, Any], evaluation_date: date) -> dict[str, Any]:
        return {
            "engine_version": ENGINE_VERSION,
            "evaluation_date": evaluation_date.isoformat(),
            "guideline": guideline,
            "decision": {
                "outcome": self.outcome,
                "outcome_class": self.outcome_class,
                "reason_code": self.reason_code,
                "decisive_rule_id": self.decisive_rule_id,
                "basis": self.basis,
                "explanation": self.explanation_text,
            },
            "criteria": [c.as_dict() for c in self.criteria],
        }


# ---------------------------------------------------------------- date helpers (explicit evaluation date
# only)
def age_years(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def shift_back(on: date, years: int = 0, months: int = 0, days: int = 0) -> date:
    """`on` minus years/months/days; the day is clamped to the target month's length (calendar arithmetic)."""
    y, m0 = divmod(on.year * 12 + (on.month - 1) - years * 12 - months, 12)
    return date(y, m0 + 1, min(on.day, calendar.monthrange(y, m0 + 1)[1])) - timedelta(days=days)


def parse_partial_date(text: Any) -> tuple[date, date] | None:
    """'2018', '2018-03' or '2018-03-05' -> (first day, last day) of the stated precision; None when
    unparseable."""  # noqa: E501
    if not isinstance(text, str):
        return None
    parts = text.strip()[:10].split("-")
    try:
        nums = [int(p) for p in parts]
        if len(nums) == 1:
            return date(nums[0], 1, 1), date(nums[0], 12, 31)
        if len(nums) == 2:
            return date(nums[0], nums[1], 1), date(nums[0], nums[1], calendar.monthrange(nums[0], nums[1])[1])
        d = date(nums[0], nums[1], nums[2])
        return d, d
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------- selectors
def _classify(sel: Selector, f: Fact) -> str | None:
    """None (irrelevant) | 'definite' | 'negative' | 'unresolved' — how fact `f` relates to selector `sel`."""
    if f.subject_type not in sel.subject_type_in:
        return None
    if "any" not in sel.category_in and f.category not in sel.category_in:
        return None
    unresolved_relative = False
    if f.subject_type == "family_member":
        if sel.unresolved_relationship_in and f.relationship in sel.unresolved_relationship_in:
            unresolved_relative = True  # a relative whose relevance (e.g. sex) cannot be decided
        elif sel.relationship_in and f.relationship not in sel.relationship_in:
            return None
    if not (
        "any" in sel.fact_type_in
        or (f.fact_type is not None and f.fact_type in sel.fact_type_in)
        or (f.code is not None and (f.code_system or "", f.code) in sel.codeset)
    ):
        return None
    if f.validation in ("rejected", "failed"):
        return None
    if (
        f.validation != "accepted"
    ):  # requires_review, pending, ...: a candidate that is not yet usable evidence
        return "unresolved"
    if f.assertion in ("uncertain", "conditional"):
        return "unresolved"
    if f.assertion == "negative":
        return "negative"
    if unresolved_relative:
        return "unresolved"
    if f.temporal in sel.temporal_status_in:
        return "definite"
    return "unresolved" if f.temporal == "unknown" else None


def _ref(f: Fact) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "clinical_record" if f.source == "record" else "evidence_fact", "id": f.id}
    if f.reference:
        d["source_reference"] = f.reference
    if f.doc_id:
        d["document_id"] = f.doc_id
    if f.quote:
        d["quote"] = f.quote[:300]
    return d


def _source(f: Fact) -> dict[str, Any]:
    d = {
        "type": "clinical_record" if f.source == "record" else "evidence_fact",
        "id": f.id,
        "assertion": f.assertion,
    }
    if f.onset is not None:
        d["age_at_onset"] = f.onset
    if f.doc_id:
        d["document_id"] = f.doc_id
    if f.quote:
        d["quote"] = f.quote[:300]
    return d


@dataclass(slots=True)
class SelResult:
    definite: list[Fact] = field(default_factory=list)
    unresolved: list[Fact] = field(default_factory=list)
    negative: list[Fact] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(
        default_factory=list
    )  # positive vs negative, or differing onset ages
    covered: bool | None = None  # coverage selectors only
    definite_subjects: dict[str, list[Fact]] = field(default_factory=dict)
    unresolved_only_subjects: set[str] = field(default_factory=set)

    def exists(self) -> bool | None:
        if self.covered is not None:
            return self.covered
        if any(c["type"] == "conflicting_assertions" for c in self.conflicts):
            return None
        if self.definite:
            return True
        return None if self.unresolved else False


def _select(sel: Selector, inputs: ResidentInputs, on: date) -> SelResult:
    res = SelResult()
    if sel.is_coverage:
        lo = shift_back(on, sel.lookback_years or DEFAULT_COVERAGE_YEARS)
        last = inputs.latest_record_date
        res.covered = last is not None and lo < last <= on
        return res
    for f in inputs.facts:
        kind = _classify(sel, f)
        if kind == "definite":
            res.definite.append(f)
        elif kind == "negative":
            res.negative.append(f)
        elif kind == "unresolved":
            res.unresolved.append(f)
    for f in res.definite:
        res.definite_subjects.setdefault(f.subject_key, []).append(f)
    res.unresolved_only_subjects = {f.subject_key for f in res.unresolved} - set(res.definite_subjects)
    for neg in res.negative:
        pos = res.definite_subjects.get(neg.subject_key)
        if pos:
            res.conflicts.append(
                {
                    "type": "conflicting_assertions",
                    "subject": neg.subject_key,
                    "sources": [_source(f) for f in [*pos, neg]],
                }
            )
    for key, facts in sorted(res.definite_subjects.items()):
        onsets = {f.onset for f in facts if f.onset is not None}
        if len(onsets) > 1:
            res.conflicts.append(
                {
                    "type": "conflicting_age_at_onset",
                    "subject": key,
                    "values": sorted(onsets),
                    "sources": [_source(f) for f in facts if f.onset is not None],
                }
            )
    return res


# ---------------------------------------------------------------- interval arithmetic for aggregates
Interval = tuple[float, float]


def _cmp(op: str, a: Interval, b: Interval) -> bool | None:
    """Compare two closed intervals; True/False only when every pair of points agrees, else None (unknown)."""
    alo, ahi = a
    blo, bhi = b
    if op == "GREATER_THAN":
        return True if alo > bhi else False if ahi <= blo else None
    if op == "GREATER_THAN_OR_EQUAL":
        return True if alo >= bhi else False if ahi < blo else None
    if op == "LESS_THAN":
        return True if ahi < blo else False if alo >= bhi else None
    if op == "LESS_THAN_OR_EQUAL":
        return True if ahi <= blo else False if alo > bhi else None
    if op in ("EQUALS", "NOT_EQUALS"):
        eq = True if (alo == ahi == blo == bhi) else False if (ahi < blo or alo > bhi) else None
        return eq if op == "EQUALS" or eq is None else not eq
    if op in ("IN", "NOT_IN"):
        if alo != ahi:
            return None
        inside = alo in b if isinstance(b, (list, tuple, set, frozenset)) else None
        return inside if op == "IN" else (None if inside is None else not inside)
    raise ValueError(f"unsupported operator {op}")


def _fmt(iv: Interval) -> str:
    lo, hi = iv
    f = lambda x: "no limit" if x in (-INF, INF) else str(int(x))  # noqa: E731
    return f(lo) if lo == hi else f"between {f(lo)} and {f(hi)}"


_OP_WORDS = {
    "EQUALS": "equals",
    "NOT_EQUALS": "does not equal",
    "GREATER_THAN": "is greater than",
    "GREATER_THAN_OR_EQUAL": "is at least",
    "LESS_THAN": "is less than",
    "LESS_THAN_OR_EQUAL": "is at most",
    "IN": "is one of",
    "NOT_IN": "is not one of",
}


def _op(op: str) -> str:
    return _OP_WORDS.get(op, op.lower().replace("_", " "))


def _shown(value: object) -> str:
    return ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)


# ---------------------------------------------------------------- expression evaluation
class _Eval:
    """Per-resident evaluation context; selector results are computed once and shared by every rule."""

    def __init__(self, definition: GuidelineDefinition, inputs: ResidentInputs, on: date) -> None:
        self.d, self.inputs, self.on = definition, inputs, on
        self._sel: dict[str, SelResult] = {}

    def sel(self, name: str) -> SelResult:
        if name not in self._sel:
            self._sel[name] = _select(self.d.selectors[name], self.inputs, self.on)
        return self._sel[name]

    # -- values
    # ---------------------------------------------------------------------------------------------------
    def age(self) -> int | None:
        birth = self.inputs.resident.birth_date
        if birth is None:
            return None
        if birth > self.on:
            raise ValueError("date of birth is after the evaluation date")
        return age_years(birth, self.on)

    def aggregate(self, name: str, agg: str, other: str | None) -> Interval | None:
        """The aggregate over a selector's subjects as an interval; None = unknown."""
        s = self.sel(name)
        definite, unresolved = s.definite_subjects, s.unresolved_only_subjects
        if agg == "distinct_subjects":
            return float(len(definite)), float(len(definite) + len(unresolved))
        if agg == "shared_subjects":
            o = self.sel(other or "")
            lo = len(set(definite) & set(o.definite_subjects))
            hi = len((set(definite) | unresolved) & (set(o.definite_subjects) | o.unresolved_only_subjects))
            return float(lo), float(hi)
        if agg in ("min_age_at_onset", "early_start_age"):
            if agg == "early_start_age" and unresolved:
                return None  # a relative who may or may not count: the start age cannot be determined
            known: list[int] = []
            uncertain = bool(unresolved)
            for facts in definite.values():
                onsets = {f.onset for f in facts if f.onset is not None}
                if len(onsets) == 1:
                    known.append(next(iter(onsets)))
                else:
                    uncertain = True  # missing age, or two different ages across documents
            if not definite and not unresolved:
                lo = hi = INF  # no subjects at all: the minimum is +infinity
            else:
                hi = float(min(known)) if known else INF
                lo = -INF if uncertain else hi
            if agg == "min_age_at_onset":
                return lo, hi
            return min(40.0, lo - 10), min(40.0, hi - 10)  # draft convention: min(40, youngest onset - 10)
        raise ValueError(f"unsupported aggregate {agg}")

    def interval_of(self, v: Any) -> Interval | None:
        if isinstance(v, FieldRef):
            pf = parse_field(v.field)
            assert pf is not None  # validated at parse time
            return self.aggregate(*pf)
        if isinstance(v, int):
            return float(v), float(v)
        return None

    # -- nodes
    # ------------------------------------------------------------------------------------------------------
    def node(self, node: Any) -> Tri:
        if isinstance(node, Leaf):
            return self.leaf(node)
        if isinstance(node, NotGroup):
            inner = self.node(node.child)
            return Tri(
                None if inner.value is None else not inner.value,
                f"NOT({inner.text})",
                inner.refs,
                inner.review,
                inner.conflicts,
            )
        results = [self.node(c) for c in node.conditions]
        is_and = isinstance(node, AndOrGroup) and node.operator == "AND"
        decisive = [r for r in results if r.value is (False if is_and else True)]
        unknown = [r for r in results if r.value is None]
        if decisive:
            return Tri(
                not is_and,
                ("AND" if is_and else "OR") + ": " + decisive[0].text,
                _merge(decisive),
                False,
                _conflicts(decisive),
            )
        if unknown:
            return Tri(
                None,
                ("AND" if is_and else "OR") + ": " + unknown[0].text,
                _merge(unknown),
                any(r.review for r in unknown),
                _conflicts(unknown),
            )
        return Tri(
            is_and,
            "AND: all conditions hold" if is_and else "OR: no alternative holds",
            _merge(results),
            False,
            _conflicts(results),
        )

    def leaf(self, lf: Leaf) -> Tri:
        op, name = lf.operator, lf.field
        if name == AGE_FIELD:
            age = self.age()
            if age is None:
                return Tri(None, "date of birth not available")
            rhs = self.interval_of(lf.value) if not isinstance(lf.value, list) else None
            if isinstance(lf.value, list):
                ok = _cmp(op, (float(age), float(age)), lf.value)  # type: ignore[arg-type]
            elif rhs is None:
                return Tri(None, "comparison value is unknown")
            else:
                ok = _cmp(op, (float(age), float(age)), rhs)
            shown = _fmt(rhs) if isinstance(lf.value, FieldRef) else _shown(lf.value)
            return Tri(
                ok,
                f"Age {age} {_op(op)} {shown}: {_tri(ok)}",
                [{"type": "resident_attribute", "attribute": "birth_date"}],
                False,
                [],
            )
        if name == SEX_FIELD:
            sex = self.inputs.resident.sex
            if sex is None or sex == "unknown":
                return Tri(None, "sex assigned at birth not recorded")
            inside = sex in lf.value if isinstance(lf.value, list) else sex == lf.value
            ok = inside if op in ("EQUALS", "IN") else not inside
            return Tri(
                ok,
                f"Sex recorded as {sex} {_op(op)} {_shown(lf.value)}: {_tri(ok)}",
                [{"type": "resident_attribute", "attribute": "administrative_sex"}],
            )
        if name == "evidence.selector":
            sname = str(lf.value)
            s = self.sel(sname)
            ex = s.exists()
            if s.covered is not None:
                text = f"{'a' if ex else 'no'} clinical record within the coverage window"
                refs: list[dict[str, Any]] = [
                    {"type": "resident_attribute", "attribute": "latest_clinical_record"}
                ]
            else:
                refs = [_ref(f) for f in [*s.definite, *s.unresolved]]
                if ex is True:
                    text = f"{len(s.definite)} accepted matching fact(s) for {sname}"
                elif ex is None:
                    text = f"{sname}: " + (
                        "conflicting statements"
                        if s.conflicts
                        else "only unresolved evidence (uncertain, conditional, not yet accepted or undecidable relative)"  # noqa: E501
                    )
                else:
                    text = f"no documented {sname}"
            if op == "NOT_EXISTS" and ex is not None:
                ex = not ex
            return Tri(ex, text, refs, ex is None, list(s.conflicts))
        sname, agg, other = lf.parsed_field  # type: ignore[misc]
        if agg == "latest_event_date":
            return self._date_within(sname, lf)
        left = self.aggregate(sname, agg, other)
        rhs = None if isinstance(lf.value, list) else self.interval_of(lf.value)
        s = self.sel(sname)
        conflicts = list(s.conflicts)
        if left is None or (rhs is None and not isinstance(lf.value, list)):
            return Tri(
                None, f"{_label(sname, agg)} is unknown", [_ref(f) for f in s.unresolved], True, conflicts
            )
        ok = _cmp(op, left, lf.value if isinstance(lf.value, list) else rhs)  # type: ignore[arg-type]
        refs = [_ref(f) for f in [*s.definite, *s.unresolved]]
        if ok is None:
            return Tri(
                None,
                f"{_label(sname, agg)} is {_fmt(left)}; cannot decide whether it {_op(op)} {_shown(lf.value) if isinstance(lf.value, list) else _fmt(rhs)}",  # noqa: E501
                refs,
                True,
                conflicts,
            )  # type: ignore[arg-type]
        return Tri(
            ok,
            f"{_label(sname, agg)} is {_fmt(left)}, which {_op(op)} {_shown(lf.value) if isinstance(lf.value, list) else _fmt(rhs)}: {_tri(ok)}",  # noqa: E501
            refs,
            False,
            [],
        )  # type: ignore[arg-type]

    def _date_within(self, name: str, lf: Leaf) -> Tri:
        s = self.sel(name)
        dur: Duration = lf.value  # type: ignore[assignment]
        lo = shift_back(self.on, dur.years, dur.months, dur.days)
        if not s.definite:
            return Tri(None, f"no documented {name}")
        inside, undetermined = [], 0
        for f in s.definite:
            if f.date_start is None or f.date_end is None:
                undetermined += 1
            elif f.date_start > lo and f.date_end <= self.on:
                inside.append(f)
            elif f.date_end <= lo or f.date_start > self.on:
                continue
            else:
                undetermined += 1  # partial date straddles the window edge
        window = (
            f"within the last {dur.years}y {dur.months}m {dur.days}d".replace(" 0y", "")
            .replace(" 0m", "")
            .replace(" 0d", "")
        )
        if inside:
            return Tri(True, f"{name} {window}", [_ref(f) for f in inside])
        if undetermined:
            return Tri(
                None,
                f"{name}: a documented event has no usable date for the window",
                [_ref(f) for f in s.definite],
                True,
            )
        return Tri(False, f"{name} not {window}", [_ref(f) for f in s.definite])


def _label(sname: str, agg: str) -> str:
    return f"{sname.replace('_', ' ')} ({agg.replace('_', ' ')})"


def _tri(v: bool | None) -> str:
    return "unknown" if v is None else str(v).lower()


def _merge(results: list[Tri]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for r in results:
        for ref in r.refs:
            seen[(ref["type"], ref.get("id") or ref.get("attribute", ""))] = ref
    return [seen[k] for k in sorted(seen)]


def _conflicts(results: list[Tri]) -> list[dict[str, Any]]:
    return [c for r in results for c in r.conflicts]


# ---------------------------------------------------------------- the evaluation
def evaluate_guideline(
    definition: GuidelineDefinition,
    inputs: ResidentInputs,
    evaluation_date: date,
    sources: Sequence[dict[str, Any]] = (),
    eligible_explanation: str | None = None,
) -> GuidelineResult:
    """Evaluate one guideline version for one resident on `evaluation_date`. Never raises: errors become
    `evaluation_error`."""  # noqa: E501
    try:
        return _evaluate(definition, inputs, evaluation_date, sources, eligible_explanation)
    except Exception as exc:  # noqa: BLE001 - a rule execution error is a recorded outcome, not a crash
        crit = CriterionResult(
            "engine.error",
            "prerequisite",
            0,
            None,
            "error",
            "evaluation_error",
            None,
            f"{type(exc).__name__}: {str(exc)[:200]}",
            "evaluation_error",
            [],
        )
        return GuidelineResult(
            "evaluation_error",
            "error",
            "evaluation_error",
            None,
            "evaluation_error",
            None,
            [crit],
            [],
            [],
            [],
            [],
            [],
        )


def _evaluate(
    d: GuidelineDefinition,
    inputs: ResidentInputs,
    on: date,
    sources: Sequence[dict[str, Any]],
    eligible_text: str | None,
) -> GuidelineResult:
    r = inputs.resident
    if r.deceased_date is not None and r.deceased_date <= on:
        crit = CriterionResult(
            "engine.resident_alive",
            "prerequisite",
            0,
            False,
            "failed",
            "not_eligible",
            None,
            f"deceased on {r.deceased_date.isoformat()}, on or before {on.isoformat()}",
            "resident_deceased",
            [{"type": "resident_attribute", "attribute": "deceased_date"}],
        )
        return GuidelineResult(
            "not_eligible",
            "ineligible",
            "resident_deceased",
            None,
            "resident_deceased",
            crit.detail,
            [crit],
            crit.refs,
            [],
            [],
            [],
            [],
        )
    ev = _Eval(d, inputs, on)
    criteria: list[CriterionResult] = []
    conflicts: list[dict[str, Any]] = []
    unknown_flags: dict[str, bool] = {}
    for rule in sorted(d.rules, key=lambda x: (x.priority, x.rule_id)):
        t = ev.node(rule.expression)
        value = t.value
        if value is None and rule.missing_data_policy == "not_satisfied":
            value = False
        key = _tri(value)
        outcome = rule.criterion_semantics.for_result(value)
        contribution = rule.outcome_by_expression_result.for_result(value)
        review_class = rule.review_class
        if value is None and rule.missing_data_policy == "review_required":
            contribution = "requires_clinical_review"
        reason = (rule.reason_code_when_unknown or rule.reason_code) if value is None else rule.reason_code
        template = (rule.explanation_templates or {}).get(key)
        detail = (template or t.text)[:400]
        criteria.append(
            CriterionResult(
                rule.rule_id,
                rule.rule_type,
                rule.priority,
                value,
                outcome,
                contribution,
                review_class,
                detail,
                reason,
                t.refs,
                t.review and value is None,
            )
        )
        conflicts.extend(t.conflicts)
        unknown_flags[rule.rule_id] = value is None
    outcome, step_rank, primary = _decide(d, criteria)
    outcome_class = OUTCOME_CLASS[outcome]
    refs = _merge([Tri(None, "", c.refs) for c in criteria])
    missing = (
        [
            {"criterion_id": c.rule_id, "reason_code": c.reason_code, "detail": c.detail}
            for c in criteria
            if c.outcome == "unknown"
        ]
        if outcome_class == "indeterminate"
        else []
    )
    exclusions = [
        {
            "rule_id": c.rule_id,
            "reason_code": c.reason_code,
            "outcome": c.contribution,
            "evidence_refs": c.refs,
        }
        for c in criteria
        if c.rule_type == "exclusion" and c.outcome == "failed"
    ]
    if outcome == "eligible":
        reason, basis, text = None, "all_criteria_met", eligible_text
    else:
        assert primary is not None
        reason, basis, text = primary.reason_code, f"decision_step_{step_rank}", primary.detail
    return GuidelineResult(
        outcome,
        outcome_class,
        reason,
        primary.rule_id if primary else None,
        basis,
        text,
        criteria,
        refs,
        missing,
        _dedupe(conflicts),
        exclusions,
        _source_refs(d, criteria, primary, sources),
    )


def _decide(
    d: GuidelineDefinition, criteria: list[CriterionResult]
) -> tuple[str, int | None, CriterionResult | None]:
    """The best-ranked contribution wins; the primary reason comes from its lowest-priority-number rule."""
    for step in d.decision_policy.steps[:-1]:
        matching = [
            c
            for c in criteria
            if c.contribution == step.outcome and step.only_rules_with_review_class in (None, c.review_class)
        ]
        if matching:
            # type: ignore[return-value]
            return step.outcome, step.rank, min(matching, key=lambda c: (c.priority, c.rule_id))
    return d.decision_policy.steps[-1].otherwise or "eligible", None, None


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for it in items:
        key = canonical_hash(it)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


_META_KEYS = ("source_status", "assumption", "pathways")


def _source_refs(
    d: GuidelineDefinition,
    criteria: list[CriterionResult],
    primary: CriterionResult | None,
    sources: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Citations behind the decision: the primary criterion's rule, else the inclusion rules (08 §4.4), with
    the rule's
    source metadata (publication dates, recommendation status, applicability, interval, grade) as the draft
    states it."""
    by_id = {s.get("source_id"): s for s in sources}
    wanted = [primary.rule_id] if primary else [c.rule_id for c in criteria if c.rule_type == "inclusion"]
    out: list[dict[str, Any]] = []
    for rule in d.rules:
        if rule.rule_id not in wanted:
            continue
        extra = rule.model_extra or {}
        refs = []
        for raw in extra.get("source_refs", []) or []:
            sid, _, excerpt = str(raw).partition("#")
            src = by_id.get(sid, {})
            refs.append(
                {
                    k: v
                    for k, v in {
                        "source_id": sid,
                        "excerpt_id": excerpt or None,
                        "title": src.get("title"),
                        "url": src.get("url"),
                        "publication_date": src.get("publication_date"),
                        "citation": src.get("citation"),
                        "grade_or_strength": src.get("grade_or_strength"),
                    }.items()
                    if v is not None
                }
            )
        meta = {k: extra[k] for k in _META_KEYS if k in extra}
        if isinstance(extra.get("source_metadata"), dict):
            meta["source_metadata"] = extra["source_metadata"]
        out.append({"rule_id": rule.rule_id, "references": refs, **({"metadata": meta} if meta else {})})
    return out


# ---------------------------------------------------------------- input snapshot / hash (incremental
# evaluation)
@dataclass(frozen=True, slots=True)
class GuidelineDeps:
    """The inputs a guideline version can depend on (08 §15.2); computed once, reused for every resident."""

    uses_birth_date: bool
    uses_sex: bool
    selectors: tuple[tuple[str, Selector], ...]
    coverage_years: int | None

    @classmethod
    def of(cls, d: GuidelineDefinition) -> GuidelineDeps:
        used = sorted({n for r in d.rules for n in r.selectors_used()})
        leaves = [lf for r in d.rules for lf in leaves_of(r.expression)]
        cov = [
            d.selectors[n].lookback_years or DEFAULT_COVERAGE_YEARS
            for n in used
            if d.selectors[n].is_coverage
        ]
        return cls(
            any(lf.field == AGE_FIELD for lf in leaves),
            any(lf.field == SEX_FIELD for lf in leaves),
            tuple((n, d.selectors[n]) for n in used if not d.selectors[n].is_coverage),
            cov[0] if cov else None,
        )

    def needs(self, f: Fact) -> bool:
        return any(_classify(sel, f) is not None for _, sel in self.selectors)


def input_snapshot(
    deps: GuidelineDeps, content_hash: str, inputs: ResidentInputs, evaluation_date: date
) -> dict[str, Any]:
    r = inputs.resident
    facts = sorted(
        (
            [
                f.id,
                f.source,
                f.category,
                f.fact_type,
                f.code_system,
                f.code,
                f.subject_type,
                f.relationship,
                f.subject_key,
                f.assertion,
                f.temporal,
                f.validation,
                f.date_start.isoformat() if f.date_start else None,
                f.date_end.isoformat() if f.date_end else None,
                f.onset,
            ]
            for f in inputs.facts
            if deps.needs(f)
        ),
        key=lambda row: (row[0], row[1]),
    )
    covered = None
    if deps.coverage_years is not None:
        lo = shift_back(evaluation_date, deps.coverage_years)
        last = inputs.latest_record_date
        covered = last is not None and lo < last <= evaluation_date
    return {
        "engine": ENGINE_VERSION,
        "input_schema": INPUT_SCHEMA_VERSION,
        "guideline_hash": content_hash,
        "evaluation_date": evaluation_date.isoformat(),
        "resident": {
            "id": str(r.id),
            "birth_date": r.birth_date.isoformat() if deps.uses_birth_date and r.birth_date else None,
            "sex": r.sex if deps.uses_sex else None,
            "deceased_date": r.deceased_date.isoformat() if r.deceased_date else None,
        },
        "covered": covered,
        "facts": facts,
    }


def input_hash(deps: GuidelineDeps, content_hash: str, inputs: ResidentInputs, evaluation_date: date) -> str:
    return canonical_hash(input_snapshot(deps, content_hash, inputs, evaluation_date))


__all__ = [
    "CLINICAL_REVIEW_STATUSES",
    "ENGINE_VERSION",
    "REVIEW_DONE",
    "REVIEW_PENDING",
    "CriterionResult",
    "Fact",
    "GuidelineDeps",
    "GuidelineResult",
    "ResidentFacts",
    "ResidentInputs",
    "Rule",
    "age_years",
    "evaluate_guideline",
    "input_hash",
    "parse_partial_date",
    "record_to_fact",
    "shift_back",
]
