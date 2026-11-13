"""The pilot places (ADR-068): the district map config and the scope a request may ask for (a state or a district).
`resolve_scope` turns `district` / `state` query values into the district codes to keep, or None for everything.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from anticipa.config import ROOT_DIR
from anticipa.core.errors import NotFound, ValidationFailed

GEOGRAPHY_FILE = ROOT_DIR / "config" / "geography" / "district_map.json"


@lru_cache
def district_map() -> dict[str, Any]:
    return json.loads(GEOGRAPHY_FILE.read_text(encoding="utf-8"))


def find_state(code: str) -> dict[str, Any]:
    c = code.strip().upper()
    for s in district_map()["states"]:
        if c in (s["code"].upper(), s["iso_3166_2"].upper(), s["iso_3166_2"].upper().removeprefix("IN-")):
            return s
    raise NotFound(f"No state or union territory with code {code!r}.")


def find_district(code: str) -> dict[str, Any]:
    c = code.strip().upper()
    for d in district_map()["districts"]:
        if c == d["code"].upper() or (d["lgd_district_code"] and c == str(d["lgd_district_code"]).upper()):
            return d
    raise NotFound(f"No pilot district with code {code!r}.")


def resolve_scope(district: str | None = None, state: str | None = None) -> tuple[str, ...] | None:
    """District codes to keep: one district, all districts of a state, or None (no filter). A district outside the
    given state is a validation error; a state without a pilot district gives an empty scope (zero residents)."""
    d = find_district(district) if district else None
    s = find_state(state) if state else None
    if d and s and d["state_code"] != s["code"]:
        raise ValidationFailed("The district is not in that state.", [{"field": "district", "reason": f"{d['code']} is in {d['state']}"}])
    if d:
        return (d["code"],)
    if s:
        return tuple(x["code"] for x in district_map()["districts"] if x["state_code"] == s["code"])
    return None


def scope_info(district: str | None, state: str | None) -> dict[str, Any]:
    """Describes the applied scope for the response (what the map selection was)."""
    codes = resolve_scope(district, state)
    d = find_district(district) if district else None
    s = find_state(state) if state else None
    label = d["district"] if d else s["name"] if s else "All districts"
    return {"district": d["code"] if d else None, "state": s["code"] if s else None, "districts": list(codes) if codes is not None else None, "label": label}


ACRONYMS = frozenset({"brca", "hpv", "via", "cbe", "crc", "fit", "tb", "hiv"})  # words shown in capitals in a programme name


def programme_label(care_activity: str) -> str:
    """Display name of a programme (care activity key): words separated, the first capitalised and known acronyms in capitals,
    for example brca_risk_assessment -> BRCA risk assessment and india_diabetes_screening -> India diabetes screening."""
    words = [w.upper() if w.lower() in ACRONYMS else w.lower() for w in care_activity.replace("-", "_").split("_") if w]
    if words and words[0].islower():
        words[0] = words[0].capitalize()
    return " ".join(words)
