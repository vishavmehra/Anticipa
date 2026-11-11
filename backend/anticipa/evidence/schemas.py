"""Model-output schema for evidence extraction (docs/17 §6, field names per 07 §6.3).

Closed objects only (extra="forbid") so strict structured-output modes on both providers can enforce it.
No numeric/length constraints in the schema: range checks happen in the acceptance policy, where a
violation is reviewed, not fatal.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "1"

Category = Literal[
    "condition",
    "family_history",
    "procedure",
    "observation",
    "immunization",
    "diagnostic_finding",
    "symptom",
    "risk_factor",
    "medication",
    "preventive_care",
]
SubjectType = Literal["resident", "family_member", "other", "unknown"]
Relationship = Literal[
    "mother",
    "father",
    "sister",
    "brother",
    "daughter",
    "son",
    "spouse",
    "maternal_grandmother",
    "maternal_grandfather",
    "paternal_grandmother",
    "paternal_grandfather",
    "maternal_aunt",
    "maternal_uncle",
    "paternal_aunt",
    "paternal_uncle",
    "cousin",
    "half_sister",
    "half_brother",
    "other_relative",
]
AssertionStatus = Literal["positive", "negative", "uncertain", "conditional"]
TemporalStatus = Literal["current", "historical", "planned", "unknown"]
Confidence = Literal["low", "medium", "high"]
Language = Literal["en", "hi", "ar", "other"]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceValue(_Closed):
    concept: str  # clinical concept in plain English (translated when the document is Hindi/Arabic)
    original_text: str | None  # the document's own words naming the concept
    detail: str | None  # qualifier: site, laterality, type, result wording
    numeric_value: float | None
    unit: str | None
    event_date: str | None  # exactly as written (or ISO if unambiguous); null when not stated


class EvidenceCandidate(_Closed):
    category: Category
    fact_type: str  # short snake_case label, e.g. breast_cancer, colonoscopy, smoking_status
    value: EvidenceValue
    subject_type: SubjectType
    relationship: Relationship | None  # required when subject_type == "family_member"
    assertion_status: AssertionStatus
    temporal_status: TemporalStatus
    age_at_onset: int | None  # age at which the SUBJECT developed it, if stated
    source_quote: str  # exact text copied from the document
    extraction_confidence: Confidence  # confidence in the extraction itself, not a clinical probability
    uncertainty_note: str | None  # why the statement is uncertain/ambiguous, else null


class EvidenceExtraction(_Closed):
    document_language: Language
    facts: list[EvidenceCandidate]
