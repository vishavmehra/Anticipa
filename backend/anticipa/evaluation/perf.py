"""Cost, throughput, latency, reliability (Docs/09 §7.9 PF-*) and coverage honesty (§7.10 COV-*).

Callers: anticipa.evaluation.runner. Source of truth: `model_invocations` rows (one per Model Router call, ids only) and
`processing_jobs`; cost is `cost_usd` as recorded (estimated from config prices, NOT reconciled with a provider bill;
cache hits are logged with cost 0, so a re-run served from cache shows no new spend). Instruction: deliver a working,
tested, demonstrable Anticipa MVP including the evaluation report.
"""

from __future__ import annotations

import os
import platform
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from anticipa.evaluation.stats import latency_summary, r6, rate


def _cost(rows: Sequence[Mapping[str, Any]]) -> float:
    return sum(r["cost_usd"] or 0.0 for r in rows)


def _per_k(cost: float, n: int) -> float | None:
    return None if n == 0 else r6(cost / (n / 1000))


def hardware() -> dict[str, Any]:
    info: dict[str, Any] = {"platform": platform.platform(), "python": sys.version.split()[0], "cpus": os.cpu_count()}
    try:
        import psutil  # optional; not a project dependency

        info["ram_gb"] = round(psutil.virtual_memory().total / 2**30, 1)
    except Exception:  # noqa: BLE001
        info["ram_gb"] = None
    return info


def evaluate_perf(
    invocations: Sequence[Mapping[str, Any]], jobs: Sequence[Mapping[str, Any]], *,
    linked_invocation_ids: set[str], documents_evaluated: int, documents_failed: int, residents_in_cohort: int,
    residents_processed: int,
) -> dict[str, Any]:  # fmt: skip
    """Cost/latency/reliability of the extraction calls.

    The router stores no document id, so calls are attributed two ways: (1) JOB level, every call made by an extraction job,
    divided by the documents those jobs processed (complete: includes retries, escalations and documents with no facts); this
    is the headline; (2) SPLIT level, only calls whose id appears on a fact of the evaluated documents (documents that
    yielded no fact have no linkable call, so this undercounts)."""
    ext_jobs = [j for j in jobs if j["status"] in ("completed", "partially_completed")]
    job_ids = {j["id"] for j in ext_jobs}
    mine = [r for r in invocations if r["job_id"] in job_ids]
    docs_by_jobs = sum(j["processed_items"] or 0 for j in ext_jobs)
    linked = [r for r in invocations if r["id"] in linked_invocation_ids]
    other = len(invocations) - len(mine)
    cost, linked_cost = _cost(mine), _cost(linked)
    null_cost = sum(1 for r in mine if r["cost_usd"] is None)
    first = [r for r in mine if r["retry_count"] == 0 and not r["fallback_used"] and r["task"] == "evidence.extract"]
    retried = [r for r in mine if r["retry_count"] > 0]
    fallback = [r for r in mine if r["fallback_used"]]
    escalated = [r for r in mine if r["task"] != "evidence.extract"]
    ok = [r for r in mine if r["status"] == "succeeded" and not r["cache_hit"] and r["latency_ms"] is not None]
    by_model: dict[str, list[float]] = defaultdict(list)
    spend: dict[str, dict[str, Any]] = {}
    for r in mine:
        k = f"{r['provider']}/{r['model_id']}/{r['task']}"
        if r["status"] == "succeeded" and not r["cache_hit"] and r["latency_ms"] is not None:
            by_model[k].append(float(r["latency_ms"]))
        s = spend.setdefault(k, {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0})
        s["calls"] += 1
        s["cost_usd"] = r6(s["cost_usd"] + (r["cost_usd"] or 0.0))
        s["input_tokens"] += r["input_tokens"] or 0
        s["output_tokens"] += r["output_tokens"] or 0
    done_jobs = [j for j in ext_jobs if j["started_at"] and j["completed_at"]]
    minutes = sum((j["completed_at"] - j["started_at"]).total_seconds() / 60 for j in done_jobs)
    processed = sum(j["processed_items"] or 0 for j in done_jobs)
    errors = Counter(r["error_category"] for r in mine if r["error_category"])
    attempted = documents_evaluated
    return {
        "PF-01": {"value": _per_k(cost, docs_by_jobs), "unit": "USD per 1,000 documents", "status": "measured" if docs_by_jobs else "not_applicable",
                  "n": docs_by_jobs, "total_cost_usd": r6(cost), "calls": len(mine), "calls_of_other_jobs_excluded": other,
                  "first_attempt_cost_usd": r6(_cost(first)), "retry_cost_usd": r6(_cost(retried)),
                  "fallback_cost_usd": r6(_cost(fallback)), "escalation_cost_usd": r6(_cost(escalated)),
                  "by_provider_model_task": spend, "cost_basis": "estimated from configured prices, not reconciled with a provider bill",
                  "attribution": "all calls of the extraction jobs divided by the documents those jobs processed",
                  "metric": "extraction cost per 1,000 documents including retries, fallbacks and escalations"},  # fmt: skip
        "PF-01s": {"value": _per_k(linked_cost, attempted), "unit": "USD per 1,000 documents (evaluated split)", "n": attempted, "calls": len(linked),
                   "total_cost_usd": r6(linked_cost), "status": "measured" if attempted else "not_applicable",
                   "metric": "cost of calls linked to facts of the evaluated documents; undercounts because documents that produced no fact have no linkable call"},  # fmt: skip
        "PF-02a": {"value": _per_k(cost, residents_processed), "unit": "USD per 1,000 residents (processed)", "n": residents_processed,
                   "status": "measured" if residents_processed else "not_applicable", "metric": "job level cost per 1,000 residents with at least one processed document"},  # fmt: skip
        "PF-02b": {"value": _per_k(cost, residents_in_cohort), "unit": "USD per 1,000 residents (cohort)", "n": residents_in_cohort,
                   "status": "measured" if residents_in_cohort else "not_applicable", "metric": "same cost over all residents of the evaluated cohort"},  # fmt: skip
        "PF-03": {"value": r6(processed / minutes) if minutes else None, "unit": "documents per active minute (extraction jobs)",
                  "status": "measured" if minutes else "not_applicable", "n": len(done_jobs),
                  "metric": "throughput of completed extraction jobs"},  # fmt: skip
        "PF-04": {"overall": latency_summary([float(r["latency_ms"]) for r in ok]),
                  "by_provider_model_task": {k: latency_summary(v) for k, v in sorted(by_model.items())},
                  "status": "measured" if ok else "not_applicable", "value": None, "unit": "ms",
                  "metric": "model call latency of successful, non cached calls (nearest rank percentiles)"},  # fmt: skip
        "PF-05a": {**rate(len(retried), len(mine)), "mean_retries_per_call": r6(sum(r["retry_count"] for r in mine) / len(mine)) if mine else None, "metric": "retry rate"},
        "PF-05b": {**rate(documents_failed, attempted), "metric": "documents failed after all retries"},
        "PF-05c": {**rate(len(fallback), len(mine)), "metric": "fallback rate"},
        "PF-05d": {**rate(errors["output_validation"], len(mine)), "metric": "output validation failure rate"},
        "PF-05e": {"rate_limited": rate(errors["rate_limited"], len(mine)), "refusal": rate(errors["refusal"], len(mine)),
                   "value": None, "status": "measured", "metric": "rate limit and refusal rates"},  # fmt: skip
        "PF-06": {**rate(len(mine) - null_cost, len(mine)), "metric": "calls with a recorded cost (null cost is never counted as zero)"},
        "cache_hits": {"value": sum(1 for r in mine if r["cache_hit"]), "unit": "calls", "status": "measured",
                       "metric": "calls served from the response cache (logged with cost 0: no new spend)"},  # fmt: skip
        "PF-10": {"value": None, "status": "not_evaluated", "metric": "billing reconciliation",
                  "reason": "no provider billed amount has been entered for this run"},  # fmt: skip
    }


def coverage(*, structured: Mapping[str, int], ai_residents: int, evaluated_residents: int, labelled_residents: int) -> list[dict[str, Any]]:
    """Target vs actual completed per tier. The population target is 200,000 (owner decision, ADR-067, reduced from 1,000,000); the AI/evaluation tier target is 10,000 (ADR-064 supersedes the
    100,000 of 09 §7.10)."""
    return [
        {"tier": "resident_population", "target": 200_000, "actual_completed": structured["residents_loaded"],
         "note": f"residents loaded; {structured['residents_with_guideline_evaluations']} have a guideline evaluation", "environment": "local"},  # fmt: skip
        {"tier": "ai_evidence_cohort", "target": 10_000, "actual_completed": ai_residents,
         "note": "residents with at least one document through AI extraction (fixed at 10,000 by the owner)", "environment": "local"},
        {"tier": "intensive_evaluation", "target": 10_000, "actual_completed": evaluated_residents,
         "note": f"residents scored against planted ground truth in this run ({labelled_residents} had labels in the selected split)", "environment": "local"},  # fmt: skip
    ]
