# ruff: noqa: E501
"""Deterministic numeric grounding: every number in an answer must be a value the tools returned (or a simple
percent, rounding or difference of them), a number the user wrote, or part of a date or code. No model call.

Allowed set = every numeric leaf in the tool results, each ratio in [0, 1] also as a percentage, differences between
two values of the same field (a "gap of x points"), plus numbers found inside result strings and tool arguments.
A number written with d decimals matches when the allowed value rounds to it at d decimals (so 4.1% matches 0.0412)."""

from __future__ import annotations

import re
from typing import Any

_NUM = re.compile(r"(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![A-Za-z0-9_])")
_SCALE = re.compile(r"\s*(million|thousand|billion|m|k)\b", re.IGNORECASE)
_MULT = {"million": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3, "billion": 1e9}
_MONTH = re.compile(r"^\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.IGNORECASE)
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
MAX_DIFF_FIELD_VALUES = 60  # pairwise differences are only computed for fields with at most this many values


def _leaves(obj: Any, key: str = "") -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float)):
        return [(key, float(obj))]
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _leaves(v, str(k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out += _leaves(v, key)
    return out


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, (list, tuple)):
        return [s for v in obj for s in _strings(v)]
    return []


def _plain(text: str) -> list[float]:
    vals = []
    for m in _NUM.finditer(_DATE.sub(" ", text)):
        vals.append(float(m.group(1).replace(",", "") + (m.group(2) or "")))
    return vals


def allowed_values(results: list[dict[str, Any]], *context: str) -> set[float]:
    """Every value an answer may legitimately quote. `context` strings (the question, tool arguments) contribute
    the numbers they contain."""
    vals: set[float] = set()
    by_field: dict[str, list[float]] = {}
    for res in results:
        for key, v in _leaves(res):
            vals.add(v)
            by_field.setdefault(key, []).append(v)
            if 0 <= v <= 1:
                vals.add(v * 100)  # a ratio shown as a percentage
        for s in _strings(res):
            vals.update(_plain(s))
    for vs in by_field.values():  # gaps between two values of the same field (levels and percentage points)
        uniq = sorted(set(vs))
        if len(uniq) <= MAX_DIFF_FIELD_VALUES:
            for i, a in enumerate(uniq):
                for b in uniq[i + 1 :]:
                    vals.add(b - a)
                    if 0 <= a <= 1 and 0 <= b <= 1:
                        vals.add((b - a) * 100)
    for c in context:
        vals.update(_plain(c))
    return vals


def numbers_in(text: str) -> list[tuple[str, float, int, float]]:
    """(token, value, decimals, tolerance) for every checkable number: skips dates, codes (D07) and day-of-month
    numbers next to a month name. A trailing scale word (million, k) multiplies the value."""
    out = []
    clean = _DATE.sub(" ", text)
    for m in _NUM.finditer(clean):
        token = m.group(0)
        tail = clean[m.end() : m.end() + 12]
        if _MONTH.match(tail) or re.match(r"^\s*(?:th|st|nd|rd)\b", tail):
            continue
        dec = len(m.group(2)) - 1 if m.group(2) else 0
        value = float(m.group(1).replace(",", "") + (m.group(2) or ""))
        tol = 0.5 * 10 ** (-dec)
        sc = _SCALE.match(tail)
        if sc:
            mult = _MULT[sc.group(1).lower()]
            value, tol = value * mult, tol * mult
        out.append((token, value, dec, tol))
    return out


def ungrounded(answer: str, allowed: set[float]) -> list[str]:
    """Numbers in `answer` that no allowed value explains (empty list = grounded)."""
    bad = []
    for token, value, _dec, tol in numbers_in(answer):
        if any(abs(a - value) <= tol + 1e-9 for a in allowed):
            continue
        if value.is_integer() and 1900 <= value <= 2100:  # a year
            continue
        bad.append(token)
    return list(dict.fromkeys(bad))
