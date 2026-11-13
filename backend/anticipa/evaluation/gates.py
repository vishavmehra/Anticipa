"""CI gates (Docs/09 §11): Tier-1 deterministic invariants (zero tolerance) and Tier-3 statistical gates.

Callers: anticipa.evaluation.runner / report. Pass/fail wording is derived from these results, never written by hand.
EVERY Tier-3 THRESHOLD IS A PROPOSAL (09 §11.3, pending owner decision Q-037) - each gate says so
(`threshold_status: "proposal"`). A gate whose metric could not be measured is `not_evaluated`, which is not a pass.
Instruction: deliver a working, tested, demonstrable Anticipa MVP including the evaluation report.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LANGS = ("en", "hi", "ar")
OPS = {">=": lambda v, t: v >= t, "<=": lambda v, t: v <= t, "==": lambda v, t: v == t}

# id, metric key, comparator, overall threshold, per-language threshold (None = overall only), severity, description
STATISTICAL: tuple[tuple[str, str, str, float, float | None, str, str], ...] = (
    ("SG-01", "EX-06", ">=", 0.95, 0.90, "block", "quote verification rate (candidate view)"),
    ("SG-02", "EX-01.A", ">=", 0.95, 0.90, "block", "strict precision (accepted)"),
    ("SG-03a", "EX-02.A", ">=", 0.80, 0.75, "warn", "strict recall (accepted)"),
    ("SG-03b", "EX-02.T", ">=", 0.90, 0.85, "block", "strict recall (accepted + review)"),
    ("SG-04", "EX-08a", ">=", 0.97, 0.95, "block", "subject-attribution accuracy"),
    ("SG-05a", "SAF-01", "<=", 0.01, 0.02, "block", "resident misattribution rate"),
    ("SG-05b", "SAF-02", "<=", 0.01, 0.02, "block", "negation-flip rate"),
    ("SG-05c", "SAF-03", "<=", 0.01, 0.02, "block", "planned-as-completed rate"),
    ("SG-05d", "SAF-04", "<=", 0.01, 0.02, "block", "historical-as-current rate"),
    ("SG-05e", "SAF-06", "<=", 0.01, 0.02, "block", "missing-documentation-as-negative rate"),
    ("SG-05f", "SAF-07", "<=", 0.01, 0.02, "block", "uncertain-as-certain rate"),
    ("SG-06", "EX-09", ">=", 0.95, 0.92, "block", "assertion accuracy"),
    ("SG-07", "EX-10", ">=", 0.90, 0.85, "warn", "temporal accuracy"),
    ("SG-08a", "EX-12", "<=", 0.02, 0.03, "block", "hallucinated-fact rate (strict)"),
    ("SG-08b", "EX-13", "<=", 0.01, 0.02, "block", "adjudicated hallucination rate"),
    ("SG-09", "EX-14", "<=", 0.02, 0.03, "block", "missing-evidence false-positive rate"),
    ("SG-10a", "EL-01", ">=", 0.90, 0.85, "warn", "Mode E eligibility agreement"),
    ("SG-10b", "EL-03", "<=", 0.02, 0.03, "block", "false-ineligible rate (missed care gap)"),
    ("SG-10c", "EL-05b", "<=", 0.02, 0.03, "block", "unsafe-determinate rate"),
    ("SG-11", "EQ-06", "<=", 10.0, None, "warn", "extraction F1 gap between languages (points)"),
    ("SG-12", "RET-01", ">=", 0.80, None, "block", "retrieval Recall@10 vs keyword baseline"),
    ("SG-13", "LBL-02", "<=", 0.05, 0.05, "block", "label-error upper bound (needs human review sample)"),
    ("SG-14", "PF-05b", "<=", 0.01, None, "warn", "document failure rate after retries"),
    ("SG-15", "CP-06", "==", 1.0, None, "block", "infeasible-scenario detection"),
    ("SG-16", "PF-01", "<=", float("nan"), None, "block", "cost ceiling (budget undefined, Q-017)"),
    ("SG-17", "PF-04", "<=", float("nan"), None, "block", "latency budget (undefined, Q-017/Q-038)"),
    ("SG-18", "EQ-02", "<=", 10.0, None, "warn", "allocation parity gap by district and language (points; warn only until Q-029)"),
)  # fmt: skip

ZERO = (  # Tier 1: id, metric key, comparator, value, description
    ("INV-01", "CP-01", "==", 0, "planner hard-constraint violations"),
    ("INV-01b", "CP-09", "==", 0, "plans published without a recorded passing validation"),
    ("INV-02", "SAF-05", "==", 0, "accepted facts lacking a verified quote"),
    ("INV-03", "LC-01", "==", 0, "completed cases lacking verified completion evidence"),
    ("INV-04", "EL-02", "==", 1.0, "rule-engine agreement on the authored regression cases"),
    ("INV-05", "EL-08", "==", 0, "eligibility non-determinism"),
    ("INV-08", "CP-04", "==", 1.0, "unallocated cases without a valid reason and explanation"),
    ("INV-09", "LC-02", "==", 1.0, "invalid closure attempts rejected"),
    ("INV-12", "LC-05", "==", 0, "outreach/appointment/evidence rows not labelled simulated"),
)  # fmt: skip


def _value(metric: Mapping[str, Any] | None) -> float | None:
    if not metric or metric.get("status") in ("not_applicable", "not_evaluated"):
        return None
    if metric.get("n") == 0:  # a count over an empty population (no cases, no plans) proves nothing: never a pass
        return None
    v = metric.get("value")
    return None if v is None else float(v)


def _lang(metric: Mapping[str, Any], lang: str) -> float | None:
    cell = ((metric.get("breakdowns") or {}).get("language") or metric.get("by_language") or {}).get(lang)
    return _value(cell)


def evaluate_gates(metrics: Mapping[str, Any], *, partial: bool, checksum_verified: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for gid, key, op, thr, desc in ZERO:
        m = metrics.get(key)
        v = _value(m)
        if partial:
            res = "skipped"
        elif v is None:
            res = "not_evaluated"
        else:
            res = "pass" if OPS[op](v, thr) else "fail"
        out.append({"gate_id": gid, "tier": 1, "metric_id": key, "comparator": op, "threshold": thr, "threshold_status": "confirmed",
                    "observed": v, "result": res, "severity": "block", "description": desc})  # fmt: skip
    out.append({"gate_id": "INV-11", "tier": 1, "metric_id": "ground_truth_checksums", "comparator": "==", "threshold": True,
                "threshold_status": "confirmed", "observed": checksum_verified, "result": "pass" if checksum_verified else "not_evaluated",
                "severity": "block", "description": "ground-truth files match the sha256 recorded by the generator"})  # fmt: skip
    for gid, key, op, thr, lang_thr, sev, desc in STATISTICAL:
        m = metrics.get(key)
        v = _value(m)
        by_lang: dict[str, float | None] = {}
        if lang_thr is not None and m:
            by_lang = {lg: _lang(m, lg) for lg in LANGS}
        no_threshold = thr != thr  # NaN: budget not defined yet
        if partial:
            res = "skipped"
        elif no_threshold or v is None:
            res = "not_evaluated"
        else:
            ok = OPS[op](v, thr) and all(x is None or OPS[op](x, lang_thr) for x in by_lang.values())
            missing = lang_thr is not None and any(x is None for x in by_lang.values())
            res = "pass" if ok else ("fail" if sev == "block" else "warn")
            if ok and missing:
                res = "not_evaluated"  # overall passes but a language could not be measured: not a clean pass
        out.append({"gate_id": gid, "tier": 3, "metric_id": key, "comparator": op, "threshold": None if no_threshold else thr,
                    "threshold_per_language": lang_thr, "threshold_status": "proposal", "observed": v, "observed_by_language": by_lang,
                    "result": res, "severity": sev, "description": desc})  # fmt: skip
    return out


def summarise(gates: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for g in gates:
        counts[g["result"]] = counts.get(g["result"], 0) + 1
    return counts


def exit_code(gates: list[dict[str, Any]], *, partial: bool, profile: str = "dev") -> int:
    """0 all evaluated gates passed; 1 a blocking gate failed; 3 partial run. `release` also refuses not_evaluated blockers."""
    if partial:
        return 3
    if any(g["result"] == "fail" and g["severity"] == "block" for g in gates):
        return 1
    if profile == "release" and any(g["result"] == "not_evaluated" and g["severity"] == "block" for g in gates):
        return 1
    return 0
