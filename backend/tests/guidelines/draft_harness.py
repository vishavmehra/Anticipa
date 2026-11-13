"""Turns config/guidelines/rules_draft.json test cases into engine inputs (loaded by path from tests).

A case describes a scenario: `age`, `sex`, `evidence` items and `no_coverage`. An item either names a selector
of the
file's selector library (`sel`) or a fact directly (`fact` + `cat`), plus optional `date` (possibly partial),
`assertion`,
`temporal`, `subject` (the relative), `onset`, `validation` and `source`. Each item is turned into the fact
such a
selector would pick, so the case runs through exactly the code paths real data uses. Expected outcomes are
read from
the file and never adjusted here.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from anticipa.guidelines.evaluator import (
    Fact,
    ResidentFacts,
    ResidentInputs,
    evaluate_guideline,
    parse_partial_date,
    shift_back,
)
from anticipa.guidelines.rules import GuidelineDefinition, parse_guideline_definition, parse_rule

DRAFT_PATH = Path(__file__).resolve().parents[3] / "config" / "guidelines" / "rules_draft.json"
DEFAULT_EVALUATION_DATE = date(2026, 11, 8)  # "Regression cases use 2026-11-08" (conventions.evaluation_date)


def load_draft() -> dict[str, Any]:
    return json.loads(DRAFT_PATH.read_text(encoding="utf-8"))


def evaluation_date(draft: dict[str, Any]) -> date:
    raw = draft.get("evaluation_date") or (draft.get("conventions", {}) or {}).get("test_evaluation_date")
    return date.fromisoformat(raw) if isinstance(raw, str) and len(raw) == 10 else DEFAULT_EVALUATION_DATE


def selectors_used(guideline: dict[str, Any], library: dict[str, Any]) -> dict[str, Any]:
    names: set[str] = set()
    for raw in guideline["rules"]:
        names |= parse_rule(raw).selectors_used()
    return {n: library[n] for n in sorted(names)}


def definition_of(guideline: dict[str, Any], library: dict[str, Any]) -> GuidelineDefinition:
    return parse_guideline_definition(
        guideline["rules"], selectors_used(guideline, library), guideline.get("decision_policy")
    )


def fact_for(item: dict[str, Any], library: dict[str, Any]) -> Fact:
    rng = parse_partial_date(item.get("date")) if item.get("date") else None
    if "sel" in item:
        sel = library[item["sel"]]
        family = "family_member" in sel["subject_type_in"] and (
            item.get("subject") not in (None, "self") or sel["subject_type_in"] == ["family_member"]
        )
        category = next((c for c in sel["category_in"] if c != "any"), "procedure")
        fact_type = next((t for t in sel["fact_type_in"] if t != "any"), "any")
        relationship = item.get("subject") or (sel.get("relationship_in") or [None])[0]
        assertion = item.get("assertion", sel["assertion_status_in"][0])
        temporal = item.get("temporal", sel["temporal_status_in"][0])
    else:  # a fact named directly
        family = item.get("subject") not in (None, "self")  # subject "self" (or absent) = the resident
        category, fact_type, relationship = item["cat"], item["fact"], item.get("subject")
        assertion, temporal = item.get("assertion", "positive"), item.get("temporal", "historical")
    return Fact(
        id=str(uuid.uuid4()),
        source="fact",
        category=category,
        fact_type=fact_type,
        code_system=None,
        code=None,
        subject_type="family_member" if family else "resident",
        relationship=relationship if family else None,
        subject_key=relationship if family else "self",
        assertion=assertion,
        temporal=temporal,
        validation=item.get("validation", "accepted"),
        date_start=rng[0] if rng else None,
        date_end=rng[1] if rng else None,
        onset=item.get("onset"),
    )


def inputs_for(case: dict[str, Any], library: dict[str, Any], on: date) -> ResidentInputs:
    birth = shift_back(on, years=int(case["age"])) if case.get("age") is not None else None
    facts = [fact_for(item, library) for item in case["evidence"]]
    latest = None if case.get("no_coverage") else shift_back(on, months=6)
    return ResidentInputs(ResidentFacts(uuid.uuid4(), birth, None, case.get("sex")), facts, latest)


def run_case(guideline: dict[str, Any], case: dict[str, Any], library: dict[str, Any], on: date):
    return evaluate_guideline(
        definition_of(guideline, library),
        inputs_for(case, library, on),
        on,
        guideline.get("sources", []),
        guideline.get("eligible_explanation"),
    )


def all_cases() -> list[tuple[str, str]]:
    draft = load_draft()
    return [(g["guideline_key"], t["id"]) for g in draft["guidelines"] for t in g["test_cases"]]
