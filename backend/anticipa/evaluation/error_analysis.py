"""Dev split ONLY error analysis of extraction (view A = accepted facts, strict match), written for the AI workstream.

Callers: `python -m anticipa.evaluation errors ...` (cli.py). Reads the planted ground truth of the DEV split through
`anticipa.evaluation.gt` (there is no split option on purpose: the test split is never read here and no access ledger is
touched) and the stored pipeline results. Output is one JSON file with counts by category confusion, fact_type, temporal
confusion and error type, plus up to N example pairs with their quotes. It changes nothing in the pipeline or database.
Instruction: deliver a working, tested, demonstrable Anticipa MVP including the evaluation report.
"""

from __future__ import annotations

import os
import random
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
from sqlalchemy import select
from sqlalchemy.orm import Session

from anticipa.config import ROOT_DIR
from anticipa.evaluation import gt as gtmod
from anticipa.evaluation import readers
from anticipa.evaluation.extraction import EVAL_CATEGORIES
from anticipa.evaluation.matching import Label, Pair, Pred, match_document, overlap

DEFAULT_OUT = ROOT_DIR / "data" / "reports" / "extraction-errors-dev.json"
TOP = 40
CONTEXT = 70  # characters of document context around a quote in an example


def _label_view(label: Label) -> dict[str, Any]:
    return {"label_id": label.label_id, "category": label.category, "fact_type": label.fact_type, "subject_type": label.subject_type,
            "relationship": label.relationship, "assertion": label.assertion, "temporal": label.temporal,
            "scenarios": list(label.scenario_ids), "span": list(label.spans[0]) if label.spans else None}  # fmt: skip


def _pred_view(p: Pred) -> dict[str, Any]:
    return {"fact_id": p.fact_id, "category": p.category, "fact_type": p.fact_type, "concept": p.concept, "subject_type": p.subject_type,
            "relationship": p.relationship, "assertion": p.assertion, "temporal": p.temporal, "disposition": p.disposition,
            "certainty": p.certainty, "span": list(p.span) if p.span else None}  # fmt: skip


def _family(label: Label) -> str:
    """Fact type key for grouping: medications collapse to their first word (the ingredient), everything else is the fact_type."""
    return label.fact_type.split("_")[0] if label.category == "medication" else label.fact_type


def _attr_diffs(pair: Pair) -> list[str]:
    p, label = pair.pred, pair.label
    out = []
    if p.subject_type != label.subject_type:
        out.append("subject")
    if label.subject_type == "family_member" and p.relationship != label.relationship:
        out.append("relationship")
    if p.assertion != label.assertion:
        out.append("assertion")
    if p.temporal != label.temporal:
        out.append("temporal")
    return out


def analyse(
    sessions: Callable[[], Session], dataset_version: str, resident_ids: Sequence[str], *, examples: int = 40, seed: int = 42,
    gt_root: Path | None = None, verify: bool = True, text_of: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:  # fmt: skip
    """`text_of(document_id)` returns the document text for the example quotes (None = quotes omitted)."""
    gt = gtmod.load_ground_truth(dataset_version, resident_ids=resident_ids, split="dev", root=gt_root, verify=verify)  # dev only
    labels = [Label.from_gt(f) for f in gt.facts]
    with sessions() as s:
        docs = readers.load_documents(s, sorted({d["resident_id"] for d in gt.doc_index.values()}))
        mismatched = [i for i, rec in gt.doc_index.items() if i in docs and docs[i][1] != rec["content_sha256"]]
        if mismatched:  # labels and stored documents must be the same text (09 GT_DOCUMENT_MISMATCH)
            raise gtmod.GtDocumentMismatch(f"{len(mismatched)} document(s) differ from the hash recorded in the ground truth")
        evaluated = {i for i, (info, _) in docs.items() if info.status in readers.EVALUATED_STATUSES and i in gt.doc_index}
        preds = readers.load_predictions(s, sorted(evaluated))
        s.rollback()
    labels_by_doc: dict[str, list[Label]] = defaultdict(list)
    for label in labels:
        if label.document_id in evaluated:
            labels_by_doc[label.document_id].append(label)
    preds_by_doc: dict[str, list[Pred]] = defaultdict(list)
    for p in preds:
        preds_by_doc[p.document_id].append(p)

    total_labels = strict = 0
    miss_types: Counter = Counter()
    miss_by_category: dict[str, Counter] = defaultdict(Counter)
    labels_by_category: Counter = Counter()
    labels_by_language: Counter = Counter()
    miss_by_language: Counter = Counter()
    category_confusion: Counter = Counter()
    fact_stats: dict[tuple[str, str], Counter] = defaultdict(Counter)
    temporal_conf: Counter = Counter()
    temporal_conf_by_category: dict[str, Counter] = defaultdict(Counter)
    assertion_conf: Counter = Counter()
    subject_conf: Counter = Counter()
    attr_combos: Counter = Counter()
    fp_types: Counter = Counter()
    fp_by_category: dict[str, Counter] = defaultdict(Counter)
    fp_by_fact: dict[tuple[str, str], Counter] = defaultdict(Counter)
    fp_assertion: Counter = Counter()
    accepted_scored = 0
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)

    for doc_id in sorted(evaluated):
        doc_labels = labels_by_doc.get(doc_id, [])
        doc_preds = preds_by_doc.get(doc_id, [])
        accepted = [p for p in doc_preds if p.disposition == "accepted" and p.category in EVAL_CATEGORIES]
        accepted_scored += len(accepted)
        m = match_document(doc_labels, accepted)
        pair_of = {pr.label.label_id: pr for pr in m.pairs}
        anywhere = [p for p in doc_preds if p.span is not None]  # every verified fact, any category and disposition
        for label in doc_labels:
            if not label.expected_present:
                continue
            total_labels += 1
            labels_by_category[label.category] += 1
            labels_by_language[label.language] += 1
            key = (label.category, _family(label))
            fact_stats[key]["labels"] += 1
            pr = pair_of.get(label.label_id)
            etype, detail, partner = "correct", None, None
            if pr is not None and pr.l2:
                strict += 1
                continue
            if pr is not None and not pr.l3:
                etype, partner = "concept_mismatch", pr.pred
            elif pr is not None:
                etype, partner = "attribute_mismatch", pr.pred
                detail = _attr_diffs(pr)
                attr_combos["+".join(detail) or "none"] += 1
                assertion_conf[f"{label.assertion}>{pr.pred.assertion}"] += pr.pred.assertion != label.assertion
                subject_conf[f"{label.subject_type}>{pr.pred.subject_type}"] += pr.pred.subject_type != label.subject_type
                if pr.pred.temporal != label.temporal:
                    temporal_conf[f"{label.temporal}>{pr.pred.temporal}"] += 1
                    temporal_conf_by_category[label.category][f"{label.temporal}>{pr.pred.temporal}"] += 1
            else:
                near = [(max(overlap(p.span, sp) for sp in label.spans), p) for p in anywhere if label.spans]
                near = sorted([(ov, p) for ov, p in near if ov >= 0.5], key=lambda t: (-t[0], t[1].fact_id))
                if not near:
                    etype = "not_detected"
                else:
                    partner = near[0][1]
                    accepted_near = [p for _, p in near if p.disposition == "accepted"]
                    if accepted_near:
                        etype, partner = "category_mismatch", accepted_near[0]
                        category_confusion[f"{label.category}>{partner.category}"] += 1
                    else:
                        etype = "found_not_accepted"
                        detail = sorted({p.disposition for _, p in near})
            miss_types[etype] += 1
            miss_by_category[label.category][etype] += 1
            miss_by_language[label.language] += 1
            fact_stats[key]["strict_misses"] += 1
            fact_stats[key][etype] += 1
            buckets[("miss", etype, label.category)].append({
                "kind": "strict_miss", "error_type": etype, "detail": detail, "language": label.language, "document_id": doc_id,
                "document_type": docs[doc_id][0].document_type, "label": _label_view(label), "prediction": _pred_view(partner) if partner else None,
            })  # fmt: skip
        paired_ids = {pr.pred.fact_id: pr for pr in m.pairs}
        for p in accepted:
            pr = paired_ids.get(p.fact_id)
            if pr is not None and pr.l2:
                continue
            etype = "no_label_at_location" if pr is None else ("concept_mismatch" if not pr.l3 else "attribute_mismatch")
            fp_types[etype] += 1
            fp_by_category[p.category][etype] += 1
            fp_by_fact[(p.category, p.fact_type)][etype] += 1
            if pr is None:
                fp_assertion[f"{p.category}:{p.assertion}"] += 1
                buckets[("false_positive", etype, p.category)].append({
                    "kind": "false_positive", "error_type": etype, "detail": None, "language": docs[doc_id][0].language, "document_id": doc_id,
                    "document_type": docs[doc_id][0].document_type, "label": None, "prediction": _pred_view(p),
                })  # fmt: skip

    def top_facts() -> list[dict[str, Any]]:
        rows = [{"category": c, "fact_type": f, "labels": v["labels"], "strict_misses": v["strict_misses"],
                 "miss_rate": round(v["strict_misses"] / v["labels"], 4) if v["labels"] else None,
                 "by_error_type": {k: n for k, n in v.items() if k not in ("labels", "strict_misses")}}
                for (c, f), v in fact_stats.items() if v["strict_misses"]]  # fmt: skip
        return sorted(rows, key=lambda r: (-r["strict_misses"], r["category"], r["fact_type"]))[:TOP]

    def top_fp() -> list[dict[str, Any]]:
        rows = [{"category": c, "fact_type": f, "accepted_false_positives": sum(v.values()), "by_error_type": dict(v)} for (c, f), v in fp_by_fact.items()]
        return sorted(rows, key=lambda r: (-r["accepted_false_positives"], r["category"], r["fact_type"]))[:TOP]

    # ---- examples: round robin over the largest (kind, error type, category) buckets, seeded and deterministic
    rng = random.Random(seed)
    ordered = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    picked: list[dict[str, Any]] = []
    pools = {k: rng.sample(v, len(v)) for k, v in ordered}
    while len(picked) < examples and any(pools.values()):
        for k, _ in ordered:
            if pools[k] and len(picked) < examples:
                picked.append({**pools[k].pop(), "bucket": "/".join(k)})
    for ex in picked:
        if text_of is None:
            continue
        text = text_of(ex["document_id"])
        if text is None:
            continue
        for who in ("label", "prediction"):
            span = (ex[who] or {}).get("span")
            if span:
                ex[who]["quote"] = text[span[0]:span[1]]
        anchor = (ex["label"] or ex["prediction"] or {}).get("span")
        if anchor:
            ex["context"] = text[max(0, anchor[0] - CONTEXT): anchor[1] + CONTEXT]

    n_miss = total_labels - strict
    return {
        "schema_version": "anticipa.eval.extraction_errors.v1",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "split": "dev",
        "dev_only_notice": "Built from the DEV split only. The test split was not read. Use it to improve the extraction; never to tune on test.",
        "dataset_version": dataset_version, "ground_truth_id": gt.ground_truth_id, "ground_truth_checksums_verified": gt.checksum_verified,
        "document_hashes_checked_against_database": len(gt.doc_index), "residents": len({d["resident_id"] for d in gt.doc_index.values()}),
        "view": "A: accepted facts, strict match (concept, subject, relationship, assertion and temporal all equal), only the six labelled categories",
        "definitions": {
            "strict_miss": "a labelled fact (expected present) with no strictly matching accepted fact",
            "not_detected": "no verified fact of any category or disposition overlaps the label span",
            "category_mismatch": "an accepted fact overlaps the label but filed under another category (gold>predicted in category_confusion)",
            "found_not_accepted": "only facts that were routed to review or rejected overlap the label",
            "concept_mismatch": "an accepted fact of a compatible category overlaps the label but names a different concept (heuristic token match)",
            "attribute_mismatch": "right concept and span, but subject, relationship, assertion or temporal differs (see detail)",
            "no_label_at_location": "false positive: an accepted fact with no label at its location (may be a true but unlabelled statement)",
        },
        "totals": {"labels_scored": total_labels, "strict_correct": strict, "strict_misses": n_miss, "accepted_facts_scored": accepted_scored,
                   "accepted_false_positives": sum(fp_types.values()),
                   "strict_precision": round(strict / accepted_scored, 6) if accepted_scored else None,
                   "strict_recall": round(strict / total_labels, 6) if total_labels else None},  # fmt: skip
        "strict_misses_by_type": dict(miss_types.most_common()),
        "strict_misses_by_category": {c: {"labels": labels_by_category[c], "strict_misses": sum(v.values()), "by_error_type": dict(v.most_common())}
                                      for c, v in sorted(miss_by_category.items())},  # fmt: skip
        "strict_misses_by_language": {lg: {"labels": labels_by_language[lg], "strict_misses": miss_by_language[lg]} for lg in sorted(labels_by_language)},
        "category_confusion_gold_to_predicted": dict(category_confusion.most_common()),
        "attribute_mismatch_combinations": dict(attr_combos.most_common()),
        "temporal_confusion_gold_to_predicted": dict(temporal_conf.most_common()),
        "temporal_confusion_by_gold_category": {c: dict(v.most_common()) for c, v in sorted(temporal_conf_by_category.items())},
        "assertion_confusion_gold_to_predicted": {k: v for k, v in assertion_conf.most_common() if v},
        "subject_confusion_gold_to_predicted": {k: v for k, v in subject_conf.most_common() if v},
        "strict_misses_by_fact_type": top_facts(),
        "false_positives_by_type": dict(fp_types.most_common()),
        "false_positives_by_category": {c: dict(v.most_common()) for c, v in sorted(fp_by_category.items())},
        "false_positives_unlabelled_by_category_and_assertion": dict(fp_assertion.most_common()),
        "false_positives_by_fact_type": top_fp(),
        "examples": picked,
    }  # fmt: skip


def write_json(obj: Mapping[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(orjson.dumps(obj, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS))
    os.replace(tmp, path)
    return path


def document_text_reader(session_factory: Callable[[], Session]) -> Callable[[str], str | None]:
    """Reads a document's text from the document store (only used for the few example quotes)."""
    import uuid

    from anticipa.datafabric.docstore import get_document_store
    from anticipa.datafabric.models import Document

    store = get_document_store()

    def read(document_id: str) -> str | None:
        with session_factory() as s:
            key = s.execute(select(Document.storage_key).where(Document.id == uuid.UUID(document_id))).scalar_one_or_none()
            s.rollback()
        try:
            return store.read_text(key) if key else None
        except FileNotFoundError:
            return None

    return read
