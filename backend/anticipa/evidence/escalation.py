"""Deterministic escalation from the cheap extraction route (Haiku) to the strong one (Sonnet).

A document is re-extracted on `evidence.extract.complex` only when the Haiku result shows one of the
triggers below,
and only while the hard cap (10% of the cohort's documents, across all runs) has room. Triggers are
computed by
code from the verified result: model output never decides whether to escalate, and the Sonnet result
goes through
the same quote verification and acceptance policy as any other.
"""

from __future__ import annotations

from collections import defaultdict

from .extract import ExtractionOutcome

UNVERIFIED_QUOTE = "unverified_quote"
CONFLICTING_FAMILY_HISTORY = "conflicting_family_history"
AMBIGUOUS_RELATIVE = "ambiguous_relative_terms"
LOW_CONFIDENCE = "low_extraction_confidence"

# Policy reasons (evidence.policy) that mean "who is this about / which relative" is unclear.
_AMBIGUOUS_REASONS = frozenset(
    {
        "family_member_without_relationship",
        "family_history_not_family_member",
        "quote_mentions_relative",
        "resident_with_relationship",
        "other_with_relationship",
        "unknown_subject",
    }
)


def conflicting_fact_indices(outcome: ExtractionOutcome) -> set[int]:
    """Indices (into outcome.facts) of family-history facts that contradict each other: the same concept
    stated
    positive and negative for one relative, a positive relative against a blanket negative ("no family history
    of X"), or two ages at onset for one relative."""
    groups: dict[str, list[int]] = defaultdict(list)
    for i, vf in enumerate(outcome.facts):
        if vf.candidate.subject_type == "family_member" and vf.decision != "rejected":
            groups[vf.candidate.fact_type.strip().lower()].append(i)
    bad: set[int] = set()
    for idx in groups.values():
        cands = {i: outcome.facts[i].candidate for i in idx}
        by_relative: dict[str | None, list[int]] = defaultdict(list)
        for i, c in cands.items():
            by_relative[c.relationship].append(i)
        for same in by_relative.values():
            if {"positive", "negative"} <= {cands[i].assertion_status for i in same}:
                bad.update(same)
            ages = {
                cands[i].age_at_onset
                for i in same
                if cands[i].assertion_status == "positive" and cands[i].age_at_onset is not None
            }
            if len(ages) > 1:
                bad.update(same)
        blanket = [
            i
            for i in idx
            if cands[i].relationship == "other_relative" and cands[i].assertion_status == "negative"
        ]
        named = [
            i
            for i in idx
            if cands[i].relationship != "other_relative" and cands[i].assertion_status == "positive"
        ]
        if blanket and named:
            bad.update(blanket + named)
    return bad


def conflicting_family_facts(outcome: ExtractionOutcome) -> bool:
    return bool(conflicting_fact_indices(outcome))


def flag_conflicts_for_review(outcome: ExtractionOutcome) -> int:
    """A flagged document that was NOT escalated (cap reached, escalation failed or off) must not leave
    contradicting family facts auto-accepted: they go to the clinical review queue. Returns how many."""
    n = 0
    for i in conflicting_fact_indices(outcome):
        vf = outcome.facts[i]
        if vf.decision == "accepted":
            vf.decision = "requires_review"
            n += 1
        if CONFLICTING_FAMILY_HISTORY not in vf.reasons:
            vf.reasons.append(CONFLICTING_FAMILY_HISTORY)
    return n


def escalation_triggers(outcome: ExtractionOutcome) -> list[str]:
    """Sorted trigger names for a completed outcome (empty list = no escalation)."""
    if outcome.status != "completed":
        return []
    found: set[str] = set()
    for vf in outcome.facts:
        c = vf.candidate
        if vf.verification.status == "quote_not_found":
            found.add(UNVERIFIED_QUOTE)
        if "low_extraction_confidence" in vf.reasons:
            found.add(LOW_CONFIDENCE)
        if _AMBIGUOUS_REASONS & set(vf.reasons) or (
            c.subject_type == "family_member"
            and c.relationship == "other_relative"
            and c.assertion_status in ("positive", "uncertain")
        ):
            found.add(AMBIGUOUS_RELATIVE)
    if conflicting_family_facts(outcome):
        found.add(CONFLICTING_FAMILY_HISTORY)
    return sorted(found)


class EscalationBudget:
    """Hard cap on escalated documents. `used` holds the item keys already escalated (this and earlier
    runs), so
    resuming a document that was already counted never consumes a second slot. Single event loop: no
    locking."""

    def __init__(self, cap_total: int, used: set[str] | None = None) -> None:
        self.cap_total = cap_total
        self.used: set[str] = set(used or ())
        self.denied = 0

    def try_take(self, key: str) -> bool:
        if key in self.used:
            return True
        if len(self.used) >= self.cap_total:
            self.denied += 1
            return False
        self.used.add(key)
        return True
