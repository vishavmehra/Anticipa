# ruff: noqa: E501  (operator tool: wide summary literals)
"""Dev-split sample comparison of two extraction prompts on the SAME documents (owner-approved: <= 400 documents,
<= $1, through the Model Router with a hard run budget).

    uv run --directory backend python -m anticipa.evidence.devcompare --dataset-version syn-20261108-s42 \\
        --cohort-manifest data/documents/cohorts/<id>.json --prompt evidence.extract@4 --docs 400 --budget 1.0 [--run]

Without --run it only selects the sample and scores the STORED facts (baseline). With --run the candidate prompt is
executed on the sample in memory (nothing is written to evidence_facts), the deterministic normaliser is applied to
the candidate's facts exactly as persistence would, and both are scored with the evaluator's own functions.

Rules: the DEV split only (the test split is never loaded: `load_ground_truth(split="dev")`), the evaluator and the
labels are used as they are, and the sample is chosen by a seeded hash, not by looking at results.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from anticipa.ai.router import build_router
from anticipa.ai.sinks import DbSink
from anticipa.datafabric.models import Document
from anticipa.db import get_sessionmaker
from anticipa.evaluation import gt as gtmod
from anticipa.evaluation import readers
from anticipa.evaluation.extraction import evaluate_extraction
from anticipa.evaluation.matching import Label, Pred
from anticipa.evidence import service
from anticipa.evidence.extract import DocumentMeta, ExtractionOutcome, extract_document
from anticipa.evidence.normalise import normalise

HEADLINE = {
    "strict_precision": "EX-01.A", "strict_recall": "EX-02.A", "strict_f1": "EX-03.A",
    "lenient_precision": "EX-04.A.precision", "lenient_recall": "EX-04.A.recall", "lenient_f1": "EX-04.A.f1",
    "detection_recall": "EX-05.A", "taxonomy_mismatch_among_misses": "EX-05c", "assertion_accuracy": "EX-09",
    "temporal_accuracy": "EX-10", "subject_accuracy": "EX-08a", "quote_verified_rate": "EX-06", "review_rate": "EX-18a",
}  # fmt: skip


def pick_sample(doc_ids: list[str], info: dict[str, Any], n: int, seed: int) -> list[str]:
    """Seeded, stratified (document type x language) sample: round-robin over strata, hash order inside each."""
    strata: dict[tuple[str, str], list[str]] = defaultdict(list)
    for d in doc_ids:
        strata[(info[d].document_type, info[d].language)].append(d)
    for members in strata.values():
        members.sort(key=lambda d: hashlib.sha256(f"{seed}:{d}".encode()).hexdigest())
    out: list[str] = []
    queues = [strata[k] for k in sorted(strata)]
    while len(out) < n and any(queues):
        for q in queues:
            if q and len(out) < n:
                out.append(q.pop(0))
    return sorted(out)


def pred_of(outcome: ExtractionOutcome, doc: Any, doc_date: date | None) -> list[Pred]:
    preds = []
    for vf in outcome.facts:
        c, v = vf.candidate, vf.verification
        value = {"concept": c.value.concept, "unit": c.value.unit, "numeric_value": c.value.numeric_value,
                 "original_text": c.value.original_text, "event_date": c.value.event_date}  # fmt: skip
        norm = normalise(c.category, c.temporal_status, c.assertion_status, c.fact_type, value, doc_date)
        span = v.spans[0] if v.verified and v.spans else None
        preds.append(Pred(
            fact_id=str(uuid.uuid4()), resident_id=str(doc.resident_id), document_id=str(doc.id), category=norm.category,
            fact_type=c.fact_type, concept=c.value.concept, subject_type=c.subject_type, relationship=c.relationship,
            assertion=c.assertion_status, temporal=norm.temporal_status,
            disposition=readers._disposition(vf.decision),
            quote_class=readers._quote_class(v.status), span=span, event_date=c.value.event_date,
            age_at_onset=c.age_at_onset, certainty=c.extraction_confidence, provider=outcome.provider,
            model_id=outcome.model_id, prompt_version=outcome.prompt_ref, location_ambiguous=v.location_ambiguous,
        ))  # fmt: skip
    return preds


def summarise(result: dict[str, Any]) -> dict[str, Any]:
    m = result["metrics"]
    return {name: (m.get(mid) or {}).get("value") for name, mid in HEADLINE.items()}


async def run_candidate(
    prompt_ref: str, docs: list[Any], texts: dict[str, str], budget: Decimal, conc: int
) -> tuple[list[ExtractionOutcome], Any]:
    router = build_router(
        sink=DbSink(get_sessionmaker()), run_budget=budget
    )  # recorded: counts toward the cumulative cap
    sem = asyncio.Semaphore(conc)

    async def one(d: Any) -> ExtractionOutcome:
        async with sem:
            return await extract_document(
                router,
                texts[str(d.id)],
                DocumentMeta(document_id=str(d.id), document_type=d.document_type, language=d.language),
                task="evidence.extract",
                prompt_ref=prompt_ref,
            )

    try:
        return list(await asyncio.gather(*(one(d) for d in docs))), router
    finally:
        await router.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset-version", required=True)
    ap.add_argument("--cohort-manifest", required=True)
    ap.add_argument("--prompt", default="evidence.extract@4")
    ap.add_argument("--docs", type=int, default=400)
    ap.add_argument("--seed", type=int, default=20261112)
    ap.add_argument(
        "--budget", type=Decimal, default=Decimal("1.0"), help="hard run budget (USD) for the candidate run"
    )
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument(
        "--run",
        action="store_true",
        help="execute the candidate prompt (paid); otherwise only score stored facts",
    )
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--no-checksum",
        action="store_true",
        help="skip ground-truth sha256 verification (docgen is still topping up)",
    )
    args = ap.parse_args(argv)
    if args.docs > 400:
        sys.exit("refused: the approved dev sample is at most 400 documents")
    resident_ids = readers.load_cohort_manifest(args.cohort_manifest)["resident_ids"]
    gt = gtmod.load_ground_truth(
        args.dataset_version, resident_ids=resident_ids, split="dev", verify=not args.no_checksum
    )  # dev ONLY
    sessions = get_sessionmaker()
    with sessions() as s:
        docs = readers.load_documents(s, sorted({d["resident_id"] for d in gt.doc_index.values()}))
        info = {i: x for i, (x, _) in docs.items() if x.status == "extracted" and i in gt.doc_index}
        sample = pick_sample(sorted(info), info, args.docs, args.seed)
        labels_all = [Label.from_gt(f) for f in gt.facts if f.get("document_id") in set(sample)]
        stored = readers.load_predictions(s, sample)
        rows = {
            str(r.id): r
            for r in s.execute(
                select(Document).where(Document.id.in_([uuid.UUID(d) for d in sample]))
            ).scalars()
        }
        s.expunge_all()
    sample_info = {d: info[d] for d in sample}
    out: dict[str, Any] = {
        "split": "dev", "documents": len(sample), "labels": sum(1 for x in labels_all if x.expected_present),
        "by_type": {t: sum(1 for d in sample if info[d].document_type == t) for t in sorted({info[d].document_type for d in sample})},
        "baseline_prompt": sorted({p.prompt_version for p in stored if p.prompt_version}),
        "baseline": summarise(evaluate_extraction(labels_all, stored, sample_info)),
    }  # fmt: skip
    if args.run:
        store = service.get_store()
        texts = {d: store.read_text(rows[d].storage_key) for d in sample}
        outcomes, router = asyncio.run(
            run_candidate(args.prompt, [rows[d] for d in sample], texts, args.budget, args.concurrency)
        )
        cand: list[Pred] = []
        for o in outcomes:
            d = rows[o.document_id]
            cand += pred_of(o, d, d.created_on.date() if d.created_on else None)
        failed = [o for o in outcomes if o.status != "completed"]
        cost = sum((o.cost_usd for o in outcomes), Decimal(0))
        out["candidate_prompt"] = args.prompt
        out["candidate"] = summarise(evaluate_extraction(labels_all, cand, sample_info))
        out["delta"] = {
            k: (
                None
                if out["baseline"][k] is None or out["candidate"][k] is None
                else round(out["candidate"][k] - out["baseline"][k], 4)
            )
            for k in HEADLINE
        }
        out["candidate_cost"] = {
            "total_usd": str(cost), "per_document_usd": str((cost / len(sample)).quantize(Decimal("0.000001"))),
            "documents_failed": len(failed), "run_spent_usd": str(router.run_spent_usd), "budget_usd": str(args.budget),
            "input_tokens": sum(o.usage.input_tokens + o.usage.cache_read_tokens + o.usage.cache_write_tokens for o in outcomes),
            "output_tokens": sum(o.usage.output_tokens for o in outcomes),
            "facts_per_document": round(sum(len(o.facts) for o in outcomes) / len(sample), 2),
        }  # fmt: skip
    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
