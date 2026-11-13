# ruff: noqa: E501  (long string literals: documents, fixtures, scripted replies)
"""Cost / quality measurement on the 20 synthetic en/hi/ar samples (paid, opt-in; never part of the normal suite):

    ANTICIPA_LIVE_REPORT=<path.json> uv run --directory backend pytest -m live tests/evidence/test_live_measure.py -s

Routes measured: evidence.extract as configured (Nebius Qwen3-235B, Haiku fallback), the Claude Haiku fallback alone,
and evidence.extract.complex (Claude Sonnet, effort high) on six documents. Each run has its own spend guard.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
from evidence_samples import SAMPLES, Sample

from anticipa.ai.config import Candidate, RouterConfig, load_config
from anticipa.ai.router import build_router
from anticipa.ai.settings import AiSettings
from anticipa.ai.sinks import InMemorySink
from anticipa.evidence import DocumentMeta, ExtractionOutcome, extract_document
from anticipa.evidence.escalation import EscalationBudget, escalation_triggers
from anticipa.evidence.estimate import ESCALATION_SHARE

pytestmark = pytest.mark.live
COMPLEX_KEYS = [
    "en_family_history",
    "hi_family_history",
    "ar_family_history",
    "en_discharge",
    "hi_discharge",
    "ar_conflict",
]
INJECTION = re.compile(r"SYSTEM NOTE|ignore all previous", re.I)


def _only(cfg: RouterConfig, task: str, provider: str, model_id: str) -> RouterConfig:
    out = cfg.model_copy(deep=True)
    out.tasks[task].candidates = [Candidate(provider=provider, model_id=model_id)]
    return out


def _recall(sample: Sample, out: ExtractionOutcome) -> tuple[int, int, int]:
    """(planted facts found strictly, found by concept+subject only, planted total)."""
    strict = loose = 0
    for rx, subject, rel, assertion in sample.expect:
        hits = [
            f.candidate
            for f in out.facts
            if re.search(rx, f"{f.candidate.value.concept} {f.candidate.fact_type}".lower().replace("_", " "))
            and f.candidate.subject_type == subject
        ]
        loose += bool(hits)
        strict += any(
            (rel is None or c.relationship == rel) and c.assertion_status == assertion for c in hits
        )
    return strict, loose, len(sample.expect)


async def _run(
    label: str, task: str, cfg: RouterConfig | None, samples: list[Sample], budget: str, conc: int
) -> dict:
    router = build_router(settings=AiSettings(), config=cfg, sink=InMemorySink())
    router.daily_budget_usd = Decimal(budget)
    sem = asyncio.Semaphore(conc)

    async def one(s: Sample):
        async with sem:
            out = await extract_document(
                router,
                s.text,
                DocumentMeta(document_id=s.key, document_type=s.document_type, language=s.language),
                task=task,
            )
            return s, out

    try:
        results = await asyncio.gather(*(one(s) for s in samples))
    finally:
        await router.aclose()
    rows = []
    for s, out in results:
        strict, loose, planted = _recall(s, out)
        rows.append(
            {
                "doc": s.key,
                "lang": s.language,
                "type": s.document_type,
                "chars": len(s.text),
                "status": out.status,
                "error": out.error_category,
                "provider": out.provider,
                "model": out.model_id,
                "fallback": out.fallback_used,
                "input_tokens": out.usage.input_tokens,
                "cache_read": out.usage.cache_read_tokens,
                "cache_write": out.usage.cache_write_tokens,
                "output_tokens": out.usage.output_tokens,
                "cost_usd": str(out.cost_usd),
                "latency_ms": out.latency_ms,
                "repairs": out.repairs,
                "facts": len(out.facts),
                "verified": sum(f.verification.verified for f in out.facts),
                "accepted": out.count("accepted"),
                "requires_review": out.count("requires_review"),
                "rejected": out.count("rejected"),
                "planted": planted,
                "planted_found_strict": strict,
                "planted_found_concept": loose,
                "injection_facts": sum(bool(INJECTION.search(f.candidate.source_quote)) for f in out.facts)
                if s.injection
                else 0,
            }
        )
    done = [r for r in rows if r["status"] == "completed"]
    facts = sum(r["facts"] for r in done) or 1
    planted = sum(r["planted"] for r in done) or 1
    cost = [Decimal(r["cost_usd"]) for r in done]
    summary = {
        "label": label,
        "task": task,
        "documents": len(rows),
        "completed": len(done),
        "total_cost_usd": str(sum(cost, Decimal(0))),
        "mean_cost_per_doc_usd": str(round(sum(cost, Decimal(0)) / max(len(cost), 1), 6)),
        "mean_input_tokens": round(
            statistics.mean(r["input_tokens"] + r["cache_read"] + r["cache_write"] for r in done)
        ),
        "mean_output_tokens": round(statistics.mean(r["output_tokens"] for r in done)),
        "mean_latency_ms": round(statistics.mean(r["latency_ms"] for r in done)),
        "p95_latency_ms": sorted(r["latency_ms"] for r in done)[max(int(len(done) * 0.95) - 1, 0)],
        "quote_verified_rate": round(sum(r["verified"] for r in done) / facts, 3),
        "accepted_rate": round(sum(r["accepted"] for r in done) / facts, 3),
        "requires_review_rate": round(sum(r["requires_review"] for r in done) / facts, 3),
        "rejected_rate": round(sum(r["rejected"] for r in done) / facts, 3),
        "planted_recall_strict": round(sum(r["planted_found_strict"] for r in done) / planted, 3),
        "planted_recall_concept_only": round(sum(r["planted_found_concept"] for r in done) / planted, 3),
        "facts_per_doc": round(facts / max(len(done), 1), 2),
        "injection_facts": sum(r["injection_facts"] for r in rows),
        "by_language": {
            lang: round(
                sum(r["planted_found_strict"] for r in done if r["lang"] == lang)
                / max(sum(r["planted"] for r in done if r["lang"] == lang), 1),
                3,
            )
            for lang in ("en", "hi", "ar")
        },
    }
    print("MEASURE", json.dumps(summary, ensure_ascii=False))  # noqa: T201
    return {"summary": summary, "rows": rows}


async def _pipeline(samples: list[Sample], cohort_documents: int) -> dict:
    """The shipped strategy end to end: Haiku, deterministic triggers, Sonnet for at most 10% of the cohort."""
    router = build_router(settings=AiSettings(), sink=InMemorySink())
    router.daily_budget_usd = Decimal("2.00")
    cap = EscalationBudget(int(cohort_documents * ESCALATION_SHARE))
    rows = []
    for s in samples:
        meta = DocumentMeta(document_id=s.key, document_type=s.document_type, language=s.language)
        first = await extract_document(router, s.text, meta, task="evidence.extract")
        triggers = escalation_triggers(first)
        final, cost, escalated = first, first.cost_usd, False
        if triggers and cap.try_take(s.key):
            second = await extract_document(router, s.text, meta, task="evidence.extract.complex")
            cost += second.cost_usd
            escalated = True
            if second.status == "completed":
                final = second
        strict, loose, planted = _recall(s, final)
        rows.append({
            "doc": s.key, "triggers": triggers, "escalated": escalated, "cost_usd": str(cost),
            "recall_strict": strict, "planted": planted, "facts": len(final.facts),
            "unverified": sum(not f.verification.verified for f in final.facts), "accepted": final.count("accepted"),
        })  # fmt: skip
    await router.aclose()
    total = sum((Decimal(r["cost_usd"]) for r in rows), Decimal(0))
    summary = {
        "documents": len(rows),
        "documents_with_triggers": sum(bool(r["triggers"]) for r in rows),
        "escalated": sum(r["escalated"] for r in rows),
        "escalation_cap": cap.cap_total,
        "denied_by_cap": cap.denied,
        "total_cost_usd": str(total),
        "mean_cost_per_doc_usd": str(round(total / len(rows), 6)),
        "planted_recall_strict": round(
            sum(r["recall_strict"] for r in rows) / max(sum(r["planted"] for r in rows), 1), 3
        ),
        "run_spent_usd": str(router.run_spent_usd),
    }
    print("MEASURE pipeline", json.dumps(summary, ensure_ascii=False))  # noqa: T201
    return {"summary": summary, "rows": rows}


@pytest.mark.anyio
async def test_measure_extraction_cost_and_quality():
    """Haiku on all 20 samples, then the full escalation pipeline (responses are cached, so Haiku is billed once
    across reruns). Sonnet-alone profiling (the old 6-document run) only with ANTICIPA_MEASURE_COMPLEX=1."""
    settings = AiSettings()
    if not settings.anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY is not configured in .env")
    cfg = load_config()
    by_key = {s.key: s for s in SAMPLES}
    report = {
        "haiku": await _run(
            "evidence.extract (Claude Haiku, shipped route)", "evidence.extract", None, SAMPLES, "0.30", 4
        )
    }
    if os.environ.get("ANTICIPA_MEASURE_COMPLEX") == "1":
        report["complex"] = await _run(
            "evidence.extract.complex (Claude Sonnet, effort high), 6 docs",
            "evidence.extract.complex",
            _only(cfg, "evidence.extract.complex", "anthropic", "claude-sonnet-5-5"),
            [by_key[k] for k in COMPLEX_KEYS],
            "1.20",
            3,
        )
    report["pipeline"] = await _pipeline(SAMPLES, cohort_documents=len(SAMPLES))
    total = sum(Decimal(r["summary"]["total_cost_usd"]) for k, r in report.items() if k != "pipeline")
    total += Decimal(report["pipeline"]["summary"]["total_cost_usd"])
    print("MEASURE total_cost_usd (upper bound; cached replays cost 0)", total)  # noqa: T201
    out = os.environ.get("ANTICIPA_LIVE_REPORT")
    if out:
        Path(out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    assert report["haiku"]["summary"]["completed"] == report["haiku"]["summary"]["documents"], (
        "some documents failed"
    )
    assert all(r["summary"].get("injection_facts", 0) == 0 for r in report.values()), (
        "fact from the injected text"
    )
    assert report["pipeline"]["summary"]["escalated"] <= report["pipeline"]["summary"]["escalation_cap"]
    assert total < Decimal("1.00")


def _docgen_sample(per: dict[str, int], seed: int) -> list[Sample]:
    """Deterministic stratified sample (document type x language) of REAL docgen documents from the dev database
    (read-only SELECTs) and the document store. Documents only: no ground truth is read (ADR-008)."""
    from sqlalchemy import text

    from anticipa.config import get_settings
    from anticipa.datafabric.docstore import get_document_store
    from anticipa.db import make_engine

    engine = make_engine(get_settings().sqlalchemy_url(), pool_size=2)
    store, out = get_document_store(), []
    types = (
        "clinical_note",
        "discharge_summary",
        "referral_letter",
        "lab_report",
        "family_history_narrative",
    )
    with engine.connect() as c:
        for doc_type in types:
            for lang, n in per.items():
                rows = c.execute(
                    text(
                        "SELECT id, storage_key FROM anticipa.documents WHERE document_type = :t AND language = :l "
                        "ORDER BY md5(id::text || :seed) LIMIT :n"
                    ),
                    {"t": doc_type, "l": lang, "seed": str(seed), "n": n},
                ).all()
                out += [
                    Sample(
                        f"{doc_type}/{lang}/{str(r.id)[:8]}", lang, doc_type, store.read_text(r.storage_key)
                    )
                    for r in rows
                ]
    engine.dispose()
    return out


@pytest.mark.anyio
async def test_measure_on_real_docgen_documents():
    """Cost per document on a small stratified sample of docgen output (opt-in: ANTICIPA_MEASURE_DOCGEN=1).
    The sample is the 'cohort' here, so the escalation cap is 10% of it."""
    if os.environ.get("ANTICIPA_MEASURE_DOCGEN") != "1":
        pytest.skip("set ANTICIPA_MEASURE_DOCGEN=1 to measure on docgen documents (paid)")
    if not AiSettings().anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY is not configured in .env")
    from anticipa.evidence.estimate import estimate_tokens

    samples = _docgen_sample({"en": 2, "hi": 1, "ar": 1}, seed=1)
    assert len(samples) == 20, len(samples)
    result = await _pipeline(samples, cohort_documents=len(samples))
    by_type: dict[str, list] = {}
    for s, r in zip(samples, result["rows"], strict=True):
        by_type.setdefault(s.document_type, []).append(Decimal(r["cost_usd"]))
    result["summary"]["mean_chars"] = round(statistics.mean(len(s.text) for s in samples))
    result["summary"]["mean_estimated_tokens"] = round(
        statistics.mean(estimate_tokens(s.text) for s in samples)
    )
    result["summary"]["cost_by_type"] = {k: str(round(sum(v) / len(v), 6)) for k, v in by_type.items()}
    result["summary"]["trigger_counts"] = dict(Counter(t for r in result["rows"] for t in r["triggers"]))
    print("MEASURE docgen", json.dumps(result["summary"], ensure_ascii=False))  # noqa: T201
    out = os.environ.get("ANTICIPA_LIVE_REPORT")
    if out:
        Path(out).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    assert result["summary"]["escalated"] <= result["summary"]["escalation_cap"]
    assert Decimal(result["summary"]["total_cost_usd"]) < Decimal("1.00")
