# ruff: noqa: E501
"""Versioned, deterministic post-extraction normaliser (no model call, no document text).

Two documented corrections to what the extraction model labelled, applied after quote verification:

  C1  category: a result that has the shape of a measurement (a numeric value and/or a measurement unit such as
      mmHg, bpm, kg/m2, mg/dL, mmol/L, %, or a named lab/vital sign) filed under `diagnostic_finding` is an
      `observation` (lab result / vital sign). Imaging or ECG *interpretations* without a measured value stay
      `diagnostic_finding`.
  T2  temporal status: a resident's condition listed under a "History" / "Past medical history" heading
      (en/hi/ar) is historical by the meaning of that heading, whatever the model assumed.
  T1  temporal status: a completed event (procedure, screening, vaccination, result, finding) whose `event_date`
      is clearly before the document date is `historical`; one dated after it is `planned`. Conditions,
      medications and family history are never re-dated this way (a diagnosis made years ago may be ongoing).

Every change keeps the model's original value in `value["normaliser"]` (version, original category/temporal status,
rule ids) so the model output stays auditable and a later normaliser version re-derives from the originals.
Human-reviewed facts are never touched. Evaluation notes: rules were chosen from general clinical conventions and the
dev split's error analysis only; the test split is never consulted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

NORMALISER_VERSION = "n2"
RESULT_CATEGORIES = frozenset({"procedure", "preventive_care", "observation", "diagnostic_finding", "immunization"})
HISTORICAL_DAY_GAP = 7  # a day-precision event is "historical" only when it is at least this many days before the document

# Units that mean "a measured value". Matched against the unit field first, then the detail/original text.
_UNIT_RE = re.compile(
    r"(mm\s?hg|bpm|beats?\s*/?\s*min|/\s?min\b|breaths?\s*/?\s*min|kg\s*/\s*m\s?[2²]|kg/m²|mg\s*/\s*d?l|g\s*/\s*d?l|"
    r"mmol\s*/\s*(?:mol|l)|µ?mol\s*/\s*l|umol\s*/\s*l|ng\s*/\s*ml|pg\s*/\s*ml|meq\s*/\s*l|[iu]\s*/\s*l|"
    r"ml\s*/\s*min|°\s?[cf]\b|\bkg\b|\blbs?\b|\bcm\b|%|spo2|egfr)",
    re.IGNORECASE,
)
_TERM_RE = re.compile(
    r"\b(blood pressure|systolic|diastolic|heart rate|pulse|bmi|body mass index|weight|height|waist|temperature|"
    r"spo2|oxygen saturation|respiratory rate|glucose|hba1c|a1c|haemoglobin|hemoglobin|hematocrit|cholesterol|ldl|hdl|"
    r"triglycerides?|creatinine|egfr|urea|sodium|potassium|calcium|bilirubin|alt|ast|alp|tsh|psa|ferritin|"
    r"vitamin d|uric acid|albumin|platelets?|wbc|white blood cell|rbc|lipid)\b",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
# Whole-heading match only: "Family history" must NOT match.
_HISTORY_HEADING = re.compile(
    r"^(?:(?:past|previous|prior|relevant|significant)\s+)?(?:medical\s+)?history$|"
    r"^(?:(?:पिछला|चिकित्सा)\s+)?इतिहास$|"
    r"^(?:التاريخ المرضي|السوابق المرضية|التاريخ الطبي|التاريخ)$",
    re.IGNORECASE,
)
_ISO = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")
_DMY = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$")
_MDY = re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$")
_MY = re.compile(r"^([A-Za-z]+)\s+(\d{4})$")


@dataclass(frozen=True)
class Norm:
    category: str
    temporal_status: str
    rules: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.rules)


def parse_event_date(text: str | None) -> tuple[int, int | None, int | None] | None:
    """(year, month|None, day|None) for ISO and English written dates; None for anything else (hi/ar words, 'some
    years ago'): a date that cannot be read exactly is never guessed."""
    if not text:
        return None
    t = text.strip()
    if m := _ISO.match(t):
        y, mo, d = int(m[1]), m[2] and int(m[2]), m[3] and int(m[3])
        return (y, mo, d) if (mo is None or 1 <= mo <= 12) and (d is None or 1 <= d <= 31) else None
    if m := _DMY.match(t):
        mo = _MONTHS.get(m[2].lower())
        return (int(m[3]), mo, int(m[1])) if mo else None
    if m := _MDY.match(t):
        mo = _MONTHS.get(m[1].lower())
        return (int(m[3]), mo, int(m[2])) if mo else None
    if m := _MY.match(t):
        mo = _MONTHS.get(m[1].lower())
        return (int(m[2]), mo, None) if mo else None
    return None


def section_heading(text: str, start: int | None) -> str | None:
    """The heading line above `start`: the nearest preceding short line that is not a list item and does not end
    like a sentence. None when the span is on the first lines or no such line exists."""
    if start is None or start < 0 or start > len(text):
        return None
    for line in reversed(text[:start].split("\n")[:-1]):  # lines strictly above the one containing the span
        t = line.strip()
        if t and not t.startswith("-") and len(t) < 70 and not t.endswith((".", "।", ":")):
            return t
    return None


def is_measurement(value: dict[str, Any], fact_type: str) -> bool:
    unit = (value.get("unit") or "").strip()
    if value.get("numeric_value") is not None or (unit and _UNIT_RE.search(unit)):
        return True
    blob = " ".join(str(x) for x in (value.get("concept"), fact_type, value.get("original_text")) if x)
    return bool(_TERM_RE.search(blob))


def _relative_to(event: tuple[int, int | None, int | None], doc: date) -> str | None:
    """'before' | 'after' | None (same period or not clearly different) at the precision the date was written."""
    y, mo, d = event
    if d is not None and mo is not None:
        try:
            gap = (doc - date(y, mo, d)).days
        except ValueError:
            return None
        return "before" if gap >= HISTORICAL_DAY_GAP else ("after" if gap < 0 else None)
    if mo is not None:
        return "before" if (y, mo) < (doc.year, doc.month) else ("after" if (y, mo) > (doc.year, doc.month) else None)
    return "before" if y < doc.year else ("after" if y > doc.year else None)


def normalise(
    category: str,
    temporal_status: str,
    assertion_status: str,
    fact_type: str,
    value: dict[str, Any],
    document_date: date | None,
    *,
    subject_type: str = "resident",
    heading: str | None = None,
) -> Norm:
    """Apply C1/T1 to the model's original labels. Pure function of its arguments."""
    new_cat, new_temp, rules = category, temporal_status, []
    if category == "diagnostic_finding" and is_measurement(value, fact_type):
        new_cat, rules = "observation", [*rules, "C1:measurement_shape"]
    if (
        document_date is not None
        and new_cat in RESULT_CATEGORIES
        and assertion_status != "negative"
        and temporal_status in ("current", "unknown")
    ):
        event = parse_event_date(value.get("event_date"))
        rel = _relative_to(event, document_date) if event else None
        if rel == "before":
            new_temp, rules = "historical", [*rules, "T1:event_before_document"]
        elif rel == "after" and new_cat in ("procedure", "preventive_care", "immunization"):
            new_temp, rules = "planned", [*rules, "T1:event_after_document"]
    if (
        heading
        and new_cat == "condition"
        and subject_type == "resident"
        and assertion_status != "negative"
        and new_temp in ("current", "unknown")
        and _HISTORY_HEADING.match(heading.strip())
    ):
        new_temp, rules = "historical", [*rules, "T2:history_section"]
    return Norm(new_cat, new_temp, rules)


def apply_to_value(value: dict[str, Any], norm: Norm, original_category: str, original_temporal: str) -> dict[str, Any]:
    """The value dict with the audit block added (unchanged facts get no block: nothing was normalised)."""
    if not norm.changed:
        return value
    return {
        **value,
        "normaliser": {
            "version": NORMALISER_VERSION,
            "rules": norm.rules,
            "original": {"category": original_category, "temporal_status": original_temporal},
        },
    }


def originals(category: str, temporal_status: str, value: dict[str, Any]) -> tuple[str, str]:
    """The model's labels: from the audit block when a previous version already changed them, else as stored."""
    block = value.get("normaliser") or {}
    orig = block.get("original") or {}
    return orig.get("category", category), orig.get("temporal_status", temporal_status)
