"""Deterministic fact <-> label matching (Docs/09 §7.1). Pure functions, no I/O.

Callers: anticipa.evaluation.extraction. Inputs: `Label` (from a gt.fact.v1 record) and `Pred` (a persisted
evidence fact joined to its verified source span). Instruction: deliver a working, tested, demonstrable MVP
including the evaluation report.

Deviations from the letter of 09 §7.1, all forced by the real vocabularies and reported in every run:
  * L1 (detection) = same document + verified span overlap >= 0.5 + category compatible (equal, or one of the label's
    `acceptable_categories`). `fact_type` equality is NOT required: Contract 17 §6 makes `fact_type` a free-text label
    chosen by the model, so equality would score naming style, not extraction.
  * L3 (lenient) additionally needs a *concept match*: token-level comparison of the predicted fact_type/concept with
    the label's fact_type, concept name and English value strings (small synonym table, Jaccard >= 0.6 or a full
    multi-token containment). It is a heuristic, not a terminology service (no concepts.json exists), so L3/L2 numbers
    are conservative for exotic wording and generous for generic words; the report says so.
  * Medications additionally match on a shared ingredient word (>= 5 letters, not a dosage-form word), added after
    inspecting DEV results where generic names ("Ibuprofen") failed to match strength/form strings; the test split
    was not inspected.
  * Ties between equally overlapping pairs prefer a concept match, then label_id, then fact_id.
Only deterministically VERIFIED spans are used; model-reported offsets never are (09 §6.7).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

OVERLAP_MIN = 0.5
_SYNONYMS = {
    "colon": "colorectal", "bowel": "colorectal", "rectal": "colorectal", "colonic": "colorectal",
    "carcinoma": "cancer", "tumor": "cancer", "tumour": "cancer", "neoplasm": "cancer", "cancers": "cancer",
    "mammogram": "mammography", "mammographic": "mammography", "pap": "pap", "colonoscopies": "colonoscopy",
}  # fmt: skip
_STOP = frozenset({"of", "the", "and", "a", "an", "in", "with", "history", "unspecified"})
_FORM_WORDS = frozenset({"tablet", "tablets", "capsule", "injection", "injectable", "solution", "suspension", "extended", "release", "spray", "pack", "mucosal",
                         "topical", "inhalant", "powder", "cream", "ointment", "patch", "system", "intrauterine", "transdermal", "delayed", "sodium", "chloride"})  # fmt: skip
_CODE_ID = re.compile(r"^(sct|rx|loinc)_[\w-]+$")
_TOKEN = re.compile(r"[^\W_]+")


def ascii_digits(s: str) -> str:
    """Arabic-Indic / Devanagari digits -> ASCII (DC-13 variants)."""
    return "".join(str(unicodedata.digit(c)) if c.isdigit() and not c.isascii() else c for c in s)


def tokens(s: str | None) -> frozenset[str]:
    if not s:
        return frozenset()
    s = ascii_digits(unicodedata.normalize("NFKC", s)).casefold().replace("_", " ")
    out = {_SYNONYMS.get(t, t) for t in _TOKEN.findall(s)}
    return frozenset(out - _STOP)


@dataclass(frozen=True, slots=True)
class Pred:
    fact_id: str
    resident_id: str
    document_id: str
    category: str
    fact_type: str
    concept: str | None
    subject_type: str
    relationship: str | None
    assertion: str
    temporal: str
    disposition: str  # accepted | review | rejected  (09 views A / T / C)
    quote_class: str  # verified | not_found | error
    span: tuple[int, int] | None  # verified span on the ORIGINAL text, else None
    event_date: str | None = None
    age_at_onset: int | None = None
    certainty: str | None = None
    provider: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    subject_person_id: str | None = None
    location_ambiguous: bool = False
    invocation_id: str | None = None


@dataclass(frozen=True, slots=True)
class Label:
    label_id: str
    resident_id: str
    document_id: str | None
    language: str
    document_type: str
    category: str
    acceptable_categories: tuple[str, ...]
    fact_type: str
    concept_id: str | None
    names: tuple[str, ...]
    subject_type: str
    relationship: str | None
    assertion: str
    temporal: str
    event_date: dict[str, Any] | None
    age_at_onset: int | None
    spans: tuple[tuple[int, int], ...]
    expected_present: bool
    scenario_ids: tuple[str, ...]
    injected_span: tuple[int, int] | None
    conflict_group_id: str | None
    duplicate_of: str | None
    method: str
    split: str
    code: dict[str, Any] | None = None
    forbidden: tuple[dict[str, Any], ...] = field(default=(), compare=False)

    @classmethod
    def from_gt(cls, d: dict[str, Any]) -> Label:
        q = d.get("quote")
        spans = [(q["start"], q["end"])] if q else []
        spans += [(s["start"], s["end"]) for s in d.get("acceptable_quote_spans") or ()]
        names = [d["fact_type"]]
        if d.get("concept_id") and not _CODE_ID.match(d["concept_id"]):
            names.append(d["concept_id"])
        names += [v for v in (d.get("value") or {}).values() if isinstance(v, str) and v.isascii() and len(v) > 2]
        inj = d.get("injected_span")
        return cls(
            label_id=d["label_id"], resident_id=d["resident_id"], document_id=d.get("document_id"),
            language=d["language"], document_type=d.get("document_type", "unknown"), category=d["category"],
            acceptable_categories=tuple(d.get("acceptable_categories") or ()), fact_type=d["fact_type"],
            concept_id=d.get("concept_id"), names=tuple(dict.fromkeys(names)),
            subject_type=d.get("subject_type") or d["subject"], relationship=d.get("relationship"),
            assertion=d["assertion"], temporal=d["temporal"], event_date=d.get("event_date"),
            age_at_onset=d.get("age_at_onset"), spans=tuple(spans), expected_present=bool(d["expected_present"]),
            scenario_ids=tuple(d.get("scenario_ids") or ()), injected_span=(inj["start"], inj["end"]) if inj else None,
            conflict_group_id=d.get("conflict_group_id"), duplicate_of=d.get("duplicate_of"),
            method=(d.get("generation") or {}).get("method", "unknown"), split=d.get("split", "dev"),
            code=d.get("code"), forbidden=tuple(d.get("forbidden_interpretations") or ()),
        )  # fmt: skip


def overlap(a: tuple[int, int], b: tuple[int, int]) -> float:
    """Overlap coefficient |A n B| / min(|A|, |B|) over half-open code-point spans (09 §6.7)."""
    inter = min(a[1], b[1]) - max(a[0], b[0])
    smallest = min(a[1] - a[0], b[1] - b[0])
    return 0.0 if inter <= 0 or smallest <= 0 else inter / smallest


def category_ok(p: Pred, label: Label) -> bool:
    return p.category == label.category or p.category in label.acceptable_categories


def concept_match(p: Pred, label: Label) -> bool:
    preds = [t for t in (tokens(p.fact_type), tokens(p.concept)) if t]
    labs = [t for t in (tokens(n) for n in label.names) if t]
    for pt in preds:
        for lt in labs:
            union = len(pt | lt)
            if pt == lt or (pt & lt and len(pt & lt) / union >= 0.6):
                return True
            if (len(lt) >= 2 and lt <= pt) or (len(pt) >= 2 and pt <= lt):
                return True
    if label.category == "medication":  # ingredient match: "Acetaminophen" names "Acetaminophen 325 MG Oral Tablet"
        drug = {t for t in set().union(*labs) if len(t) >= 5 and not t.isdigit()} - _FORM_WORDS if labs else set()
        if any(t in drug for pt in preds for t in pt):
            return True
    if (label.concept_id or "").endswith("unspecified"):  # "cancer_unspecified": any statement naming the generic term
        base = tokens(label.fact_type)
        return bool(base) and any(base <= pt for pt in preds)
    return False


def relationship_equal(p: Pred, label: Label) -> bool:
    return label.subject_type != "family_member" or p.relationship == label.relationship


@dataclass(slots=True)
class Pair:
    label: Label
    pred: Pred
    overlap: float
    concept_ok: bool

    @property
    def l3(self) -> bool:
        return self.concept_ok

    @property
    def l2(self) -> bool:
        p, label = self.pred, self.label
        return (
            self.concept_ok and p.subject_type == label.subject_type and relationship_equal(p, label)
            and p.assertion == label.assertion and p.temporal == label.temporal
        )  # fmt: skip


@dataclass(slots=True)
class DocMatch:
    pairs: list[Pair] = field(default_factory=list)
    false_positives: list[Pred] = field(default_factory=list)
    duplicates: list[Pred] = field(default_factory=list)
    false_negatives: list[Label] = field(default_factory=list)


def match_document(labels: list[Label], preds: list[Pred]) -> DocMatch:
    """One-to-one greedy match of one document's present-labels and predicted facts (09 §7.1 steps 1-5)."""
    present = [label for label in labels if label.expected_present]
    cands: list[tuple[float, int, str, str, Label, Pred, bool]] = []
    for label in present:
        for p in preds:
            if p.span is None or not category_ok(p, label) or not label.spans:
                continue
            ov = max(overlap(p.span, s) for s in label.spans)
            if ov >= OVERLAP_MIN:
                cm = concept_match(p, label)
                cands.append((-ov, -int(cm), label.label_id, p.fact_id, label, p, cm))
    cands.sort(key=lambda t: t[:4])
    used_l: set[str] = set()
    used_p: set[str] = set()
    out = DocMatch()
    for neg_ov, _, lid, pid, label, p, cm in cands:
        if lid in used_l or pid in used_p:
            continue
        used_l.add(lid)
        used_p.add(pid)
        out.pairs.append(Pair(label, p, -neg_ov, cm))
    compat = {c[3] for c in cands}  # predictions location-compatible with some label
    for p in preds:
        if p.fact_id in used_p:
            continue
        (out.duplicates if p.fact_id in compat else out.false_positives).append(p)
    out.false_negatives = [label for label in present if label.label_id not in used_l]
    return out
