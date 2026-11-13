"""Deterministic synthetic assignments layered on Synthea residents (contract 17 §7).

Synthea's demographics are US-based; districts and preferred languages are synthetic assignments computed from a
hash of the resident UUID, so they are reproducible and independent of generation order. The weights below are an
assumption pending Q-008 and are recorded in every dataset manifest.
"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import uuid
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable
from itertools import accumulate

DISTRICT_CODES: tuple[str, ...] = tuple(f"D{i:02d}" for i in range(1, 11))  # 10 districts, uniform
LANGUAGE_WEIGHTS: tuple[tuple[str, float], ...] = (("en", 0.60), ("hi", 0.25), ("ar", 0.15))

_LANG_CODES = tuple(code for code, _ in LANGUAGE_WEIGHTS)
_LANG_CUM = tuple(accumulate(w for _, w in LANGUAGE_WEIGHTS))


def _unit(resident_id: uuid.UUID, salt: str) -> float:
    """Uniform float in [0, 1) derived from sha256(salt:uuid)."""
    digest = hashlib.sha256(f"{salt}:{resident_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def assign_district(resident_id: uuid.UUID) -> str:
    return DISTRICT_CODES[int(_unit(resident_id, "district") * len(DISTRICT_CODES))]


def assign_language(resident_id: uuid.UUID) -> str:
    return _LANG_CODES[min(bisect_right(_LANG_CUM, _unit(resident_id, "language")), len(_LANG_CODES) - 1)]


def assignment_config() -> dict:
    """Recorded in the dataset manifest next to the realised distributions."""
    return {
        "method": "sha256(salt:resident_uuid) -> uniform [0,1) -> bucket",
        "districts": {"codes": list(DISTRICT_CODES), "weights": "uniform"},
        "languages": {code: w for code, w in LANGUAGE_WEIGHTS},
        "assumption": "pending Q-008 (Docs/14_OPEN_QUESTIONS.md)",
    }


def count_assignments(resident_ids: Iterable[uuid.UUID]) -> dict[str, dict[str, int]]:
    districts: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    for rid in resident_ids:
        districts[assign_district(rid)] += 1
        languages[assign_language(rid)] += 1
    return {"district": dict(sorted(districts.items())), "language": dict(sorted(languages.items()))}
