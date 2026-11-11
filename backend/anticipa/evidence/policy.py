"""Acceptance policy for extracted facts (docs/17 §6). Deterministic; confidence is not a clinical signal.

accepted         quote verified (exact or normalized) at one place; subject/relationship consistent
requires_review  verified but ambiguous/inconsistent/uncertain/implausible, or verification inconclusive
rejected         quote not found in the document (unsupported or fabricated) or empty content

Only verified_exact / verified_normalized facts can be accepted. Every outcome is persisted by the caller;
nothing is silently dropped and only `accepted` facts may feed rules.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from .quotes import QuoteVerification
from .schemas import EvidenceCandidate

Decision = Literal["accepted", "requires_review", "rejected"]

# Relative terms (en/hi/ar). Only catches "subject_type=resident" on a quote that talks about a relative.
# ponytail: lexical guard; misses Arabic clitic-attached and inflected Hindi forms. Upgrade: morphology.
_RELATIVE_TERMS = frozenset(
    """mother mom father dad sister brother daughter son grandmother grandfather grandma grandpa aunt uncle
    cousin parent parents sibling siblings wife husband spouse niece nephew
    माँ मां माता माताजी पिता पिताजी बहन भाई बेटी बेटा दादी दादा नानी नाना चाची चाचा मौसी मामा बुआ फूफा पत्नी पति
    भतीजा भतीजी पुत्र पुत्री
    أم أمي والدة والدتي أب أبي والد والدي أخت أختي أخ أخي ابنة ابن بنت جدة جد عم عمة خال خالة زوجة زوج
    الأم الأب الأخ الأخت الوالدة الوالد الابن الابنة الجدة الجد العم العمة الخال الخالة""".split()
)
_SPLIT = re.compile(r"[\s.,;:!?()\[\]{}\"'“”‘’،؛؟।|/\\\-–—]+")  # \w would split Devanagari vowel signs
MAX_PLAUSIBLE_AGE = 120


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reasons: tuple[str, ...]


def _mentions_relative(quote: str) -> bool:
    tokens = _SPLIT.split(unicodedata.normalize("NFC", quote).lower())
    return any(t in _RELATIVE_TERMS for t in tokens)


def decide(fact: EvidenceCandidate, verification: QuoteVerification) -> Verdict:
    if verification.status == "quote_not_found":
        return Verdict("rejected", ("quote_not_found",))
    if not fact.value.concept.strip():
        return Verdict("rejected", ("empty_concept",))

    reasons: list[str] = []
    if not verification.verified:  # span_mismatch / source_missing / verification_error: inconclusive
        reasons.append(verification.status)
    if verification.location_ambiguous:
        reasons.append("location_ambiguous")

    if fact.subject_type == "family_member":
        if fact.relationship is None:
            reasons.append("family_member_without_relationship")
    elif fact.relationship is not None:
        reasons.append(f"{fact.subject_type}_with_relationship")
    if fact.category == "family_history" and fact.subject_type != "family_member":
        reasons.append("family_history_not_family_member")
    if fact.subject_type == "unknown":
        reasons.append("unknown_subject")  # never silently attributed to the resident
    if fact.subject_type == "resident" and _mentions_relative(fact.source_quote):
        reasons.append("quote_mentions_relative")

    if fact.extraction_confidence == "low":
        reasons.append("low_extraction_confidence")
    if fact.age_at_onset is not None and not 0 <= fact.age_at_onset <= MAX_PLAUSIBLE_AGE:
        reasons.append("implausible_age")

    return Verdict("requires_review" if reasons else "accepted", tuple(reasons))
