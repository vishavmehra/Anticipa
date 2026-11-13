"""Statistics for the evaluation harness (Docs/09 §7.1; standard library only, EVR-18).

Callers: every metric module. Schema: a *rate* is {value, numerator, denominator, n, unit, ci95, status, flags}.
Instruction: deliver a working, tested, demonstrable Anticipa MVP including the evaluation report.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Any

Z95 = 1.959963984540054
LOW_N = 30  # 09 §7.1: cells with n < 30 carry `low_n` and are excluded from cross-cell gaps/ratios
ND = 6  # decimals kept in reports, so metrics_digest is stable across platforms


def r6(x: float | None) -> float | None:
    return None if x is None else round(x, ND)


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for k successes in n trials (95% by default); None when n = 0."""
    if n <= 0:
        return None
    p, z2 = k / n, z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def rate(num: int, den: int, *, unit: str = "ratio", flags: Sequence[str] = (), ci: bool = True) -> dict[str, Any]:
    """A proportion with numerator/denominator. Zero denominator -> value null, status not_applicable (never 0/1)."""
    fl = list(flags)
    if den <= 0:
        return {"value": None, "numerator": num, "denominator": 0, "n": 0, "unit": unit, "ci95": None,
                "status": "not_applicable", "flags": fl}  # fmt: skip
    if den < LOW_N:
        fl.append("low_n")
    interval = wilson(num, den) if ci else None
    return {
        "value": r6(num / den), "numerator": num, "denominator": den, "n": den, "unit": unit,
        "ci95": [r6(interval[0]), r6(interval[1])] if interval else None, "status": "measured", "flags": fl,
    }  # fmt: skip


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile: the value at rank ceil(q*n) of the sorted sample (09 §7.1)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def latency_summary(values: Sequence[float]) -> dict[str, Any]:
    """p50/p95 with n; p95 is withheld below 20 samples (09 PF-04)."""
    n = len(values)
    return {"n": n, "p50_ms": percentile(values, 0.5), "p95_ms": percentile(values, 0.95) if n >= 20 else None,
            "mean_ms": r6(sum(values) / n) if n else None, "flags": [] if n >= 20 else ["p95_withheld_n_lt_20"]}  # fmt: skip


def f1(tp: int, pred_n: int, label_n: int) -> float | None:
    den = pred_n + label_n
    return None if den == 0 else 2 * tp / den


def cluster_bootstrap_f1(
    clusters: Sequence[tuple[int, int, int]], *, seed: int, resamples: int = 1000
) -> list[float] | None:
    """95% interval of F1 = 2tp/(pred+label), resampling whole residents (documents of one resident are not
    independent, 09 §7.1). `clusters` = one (tp, predicted, labelled) triple per resident."""
    if len(clusters) < 2:
        return None
    rng = random.Random(seed)
    n = len(clusters)
    stats: list[float] = []
    for _ in range(resamples):
        tp = pn = ln = 0
        for i in rng.choices(range(n), k=n):
            c = clusters[i]
            tp, pn, ln = tp + c[0], pn + c[1], ln + c[2]
        v = f1(tp, pn, ln)
        if v is not None:
            stats.append(v)
    if not stats:
        return None
    return [r6(percentile(stats, 0.025)), r6(percentile(stats, 0.975))]  # type: ignore[list-item]
