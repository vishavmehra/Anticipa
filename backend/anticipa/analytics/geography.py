"""Geography and programme analytics for the India map (ADR-068). Residents only carry `district_code` D01 to D10; the
pilot districts and their states come from `config/geography/district_map.json` and are applied when reading, so resident
rows stay unchanged. Every figure comes from the precomputed core snapshot (real counts of the loaded residents, cases and
evaluations); states without a pilot district are listed with population 0 and "no coverage". Read-only.
"""
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from anticipa.analytics.places import district_map, find_district, find_state, programme_label, scope_info
from anticipa.analytics.service import (
    SOURCES,
    SYNTHETIC_BANNER,
    _now,
    area_metrics,
    cached,
    rollup,
    scoped_core,
    snapshot_meta,
)
from anticipa.analytics.snapshot import load_core
from anticipa.core.errors import NotFound, ValidationFailed
from anticipa.datafabric.routes import AGE_BANDS
from anticipa.planning.models import Clinic

GEOGRAPHY_TTL_S = 30.0
LEVELS = ("country", "state", "district")
NUMERIC = ("residents", "evaluated", "cases", "open", "completed", "planned", "in_outreach", "due")

DEFINITIONS: dict[str, dict[str, str]] = {
    "population": {"label": "Population", "source": "synthetic",
                   "definition": "Residents placed in the area (generated residents; the pilot district is an assigned place, not a real person's address)."},
    "due_residents": {"label": "Residents due", "source": "synthetic",
                      "definition": "Distinct residents whose latest evaluation of at least one guideline version is eligible or requires_clinical_review."},
    "open_care_gaps": {"label": "Open care gaps", "source": "synthetic",
                       "definition": "Care gap cases whose status is not completed, closed_not_completed or cancelled."},
    "residents_with_open_gaps": {"label": "Residents with open gaps", "source": "synthetic",
                                 "definition": "Distinct residents with at least one open care gap case."},
    "cases_total": {"label": "Care gaps opened", "source": "synthetic", "definition": "All care gap cases ever created in the area, any status."},
    "completed": {"label": "Gaps closed", "source": "simulated",
                  "definition": "Cases with status completed (verified completion evidence, which is simulated)."},
    "in_outreach": {"label": "In outreach", "source": "simulated",
                    "definition": "Cases whose status is outreach_pending or outreach_completed (simulated messages)."},
    "up_to_date_residents": {"label": "Residents up to date", "source": "synthetic",
                             "definition": "Distinct residents whose latest evaluation of the programme's guideline says the care is already completed."},
    "screening_coverage": {"label": "Screening coverage", "source": "synthetic",
                           "definition": "Residents up to date divided by residents up to date plus residents due, for one programme (null when neither exists). Residents the programme does not apply to are left out."},
    "gaps_per_100": {"label": "Open gaps per 100 residents", "source": "synthetic",
                     "definition": "Open care gaps divided by population, times 100 (null when the population is 0)."},
    "closure_rate": {"label": "Closure rate", "source": "simulated",
                     "definition": "Completed cases divided by all cases created (null when no case exists)."},
    "coverage": {"label": "Coverage", "source": "simulated",
                 "definition": "Cases that reached planning (a simulated slot was proposed) divided by all cases created (null when no case exists)."},
}


def _zero() -> dict[str, Any]:
    return {**dict.fromkeys(NUMERIC, 0), "stage": {"opened": 0, "planned": 0, "outreach": 0, "closed": 0, "open_gaps": 0}, "programmes": {}}


def merge(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum rollup rows (districts of a state, or every district) into one area."""
    out = _zero()
    for r in rows:
        for k in NUMERIC:
            out[k] += r[k]
        for k, v in r["stage"].items():
            out["stage"][k] = out["stage"].get(k, 0) + v
        for activity, p in r["programmes"].items():
            target = out["programmes"].setdefault(activity, dict.fromkeys(("cases", "open", "completed", "planned", "in_outreach", "due", "up_to_date"), 0))
            for k in target:
                target[k] += p[k]
    return out


def _programmes(r: dict[str, Any]) -> list[dict[str, Any]]:
    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    rows = [
        {"care_activity": a, "label": programme_label(a), "cases_total": p["cases"], "open_care_gaps": p["open"], "completed": p["completed"],
         "in_outreach": p["in_outreach"], "due_residents": p["due"], "closure_rate": ratio(p["completed"], p["cases"]), "coverage": ratio(p["planned"], p["cases"]),
         "up_to_date_residents": p["up_to_date"], "screening_coverage": ratio(p["up_to_date"], p["up_to_date"] + p["due"])}
        for a, p in r["programmes"].items()
    ]  # fmt: skip
    return sorted(rows, key=lambda x: (-x["cases_total"], x["care_activity"]))


def _area(level: str, merged: dict[str, Any], **identity: Any) -> dict[str, Any]:
    return {"level": level, **identity, **area_metrics(merged), "programmes": _programmes(merged)}


def _state_area(state: dict[str, Any], districts: list[dict[str, Any]], roll: dict[str, dict[str, Any]]) -> dict[str, Any]:
    merged = merge([roll.get(d["code"]) or _zero() for d in districts])
    pilot = bool(districts)
    return _area("state", merged, code=state["code"], iso_code=state["iso_3166_2"], name=state["name"], type=state["type"],
                 coverage_status="pilot" if pilot else "no_coverage", district_codes=[d["code"] for d in districts],
                 message=None if pilot else "No pilot district in this state: no residents are placed here.")  # fmt: skip


def _district_area(d: dict[str, Any], roll: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return _area("district", roll.get(d["code"]) or _zero(), code=d["code"], name=d["district"], state_code=d["state_code"], state=d["state"],
                 iso_code=d["iso_3166_2"], lgd_district_code=d["lgd_district_code"], lat=d["lat"], lon=d["lon"], coverage_status="pilot", message=None)  # fmt: skip


def geography(
    session: Session, *, level: str = "country", code: str | None = None, sessions: Callable[[], Session] | None = None, now: datetime | None = None
) -> dict[str, Any]:
    if level not in LEVELS:
        raise ValidationFailed("Unsupported level.", [{"field": "level", "reason": f"one of {list(LEVELS)}"}])
    if level != "country" and not code:
        raise ValidationFailed("A code is required for this level.", [{"field": "code", "reason": "state: LGD or ISO code; district: pilot code such as D01"}])
    return cached(("geography", level, (code or "").upper()), GEOGRAPHY_TTL_S, lambda: _geography(session, level, code, sessions, now or _now()))


def _geography(session: Session, level: str, code: str | None, sessions: Callable[[], Session] | None, now: datetime) -> dict[str, Any]:
    core, meta = load_core(session, sessions)
    roll = rollup(core)
    gmap = district_map()
    districts, states = gmap["districts"], gmap["states"]
    by_state: dict[str, list[dict[str, Any]]] = {s["code"]: [] for s in states}
    for d in districts:
        by_state[d["state_code"]].append(d)
    mapped = {d["code"] for d in districts}
    unmapped = {k: v for k, v in roll.items() if k not in mapped}

    if level == "country":
        area = _area("country", merge(list(roll.values())), code="IN", iso_code="IN", name="India", coverage_status="pilot")
        areas = [_state_area(s, by_state[s["code"]], roll) for s in states]
    elif level == "state":
        state = find_state(code or "")
        area = _state_area(state, by_state[state["code"]], roll)
        areas = [_district_area(d, roll) for d in by_state[state["code"]]]
    else:
        d = find_district(code or "")
        area, areas = _district_area(d, roll), []
    return {
        "level": level,
        "code": code,
        "generated_at": now,
        "cache_ttl_seconds": GEOGRAPHY_TTL_S,
        "snapshot": snapshot_meta(meta),
        "synthetic_banner": SYNTHETIC_BANNER,
        "sources": SOURCES,
        "definitions": DEFINITIONS,
        "pilot": {
            "districts": len(districts), "states": sum(1 for v in by_state.values() if v), "states_listed": len(states),
            "definition": "Residents are placed in ten pilot districts as part of the data design. The figures describe those generated residents, not the real health of the districts.",
        },
        "area": area,
        "areas": areas,
        "unmapped": None if not unmapped else {
            "district_codes": sorted(k or "unknown" for k in unmapped), **area_metrics(merge(list(unmapped.values()))),
            "definition": "Residents whose district code is not in the pilot map; included in the country totals only.",
        },
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ programmes
def programmes(
    session: Session, *, district: str | None = None, state: str | None = None, sessions: Callable[[], Session] | None = None, now: datetime | None = None
) -> dict[str, Any]:
    scope = scope_info(district, state)
    return cached(("programmes", scope["district"], scope["state"]), GEOGRAPHY_TTL_S, lambda: _programmes_payload(session, sessions, scope, now or _now()))


def _programmes_payload(session: Session, sessions: Callable[[], Session] | None, scope: dict[str, Any], now: datetime) -> dict[str, Any]:
    core, meta = load_core(session, sessions)
    core = scoped_core(core, None if scope["districts"] is None else tuple(scope["districts"]))
    total = merge(list(rollup(core).values()))
    return {
        "generated_at": now,
        "cache_ttl_seconds": GEOGRAPHY_TTL_S,
        "snapshot": snapshot_meta(meta),
        "scope": scope,
        "synthetic_banner": SYNTHETIC_BANNER,
        "sources": SOURCES,
        "definitions": {k: DEFINITIONS[k] for k in ("due_residents", "open_care_gaps", "completed", "in_outreach", "closure_rate", "coverage", "up_to_date_residents", "screening_coverage")
                        if k in DEFINITIONS} | {"programme": {"label": "Programme", "source": "synthetic", "definition": "A programme is a care activity of a guideline version, for example colorectal cancer screening."}},
        "population": total["residents"],
        "programmes": _programmes(total),
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ population explorer
COHORT_FIELDS = ("district", "age_band", "sex", "residents", "cases", "open", "completed", "in_outreach", "planned", "due", "attended")
AGE_BAND_LABELS = (*(b[0] for b in AGE_BANDS), "unknown")
SEXES = ("female", "male", "other", "unknown")
EXPLORER_NOT_AVAILABLE = {
    "risk_stratum": "No resident risk score or risk stratum exists: eligibility is decided by guideline rules, not by a risk model.",
    "rural_urban": "Residents carry no rural or urban attribute.",
    "income_or_deprivation": "Residents carry no income or deprivation index, so no quintile view can be built.",
}


def _slice(rows: list[dict[str, Any]]) -> dict[str, Any]:
    t = {k: sum(r[k] for r in rows) for k in COHORT_FIELDS[3:]}
    return {
        "population": t["residents"], "due_residents": t["due"], "open_care_gaps": t["open"], "cases_total": t["cases"], "completed": t["completed"],
        "in_outreach": t["in_outreach"], "attended": t["attended"], "gaps_per_100": round(t["open"] * 100 / t["residents"], 2) if t["residents"] else None,
        "closure_rate": round(t["completed"] / t["cases"], 4) if t["cases"] else None, "coverage": round(t["planned"] / t["cases"], 4) if t["cases"] else None,
    }  # fmt: skip


def population_explorer(
    session: Session, *, district: str | None = None, age_band: str | None = None, sex: str | None = None,
    sessions: Callable[[], Session] | None = None, now: datetime | None = None,
) -> dict[str, Any]:  # fmt: skip
    """`age_band` and `sex` take one value or a comma separated list (for example 50-59,60-69,70-79,80+ for ages 50 and over)."""
    codes = {d["code"]: d for d in district_map()["districts"]}
    district = district.upper() if district else None
    if district and district not in codes:
        raise NotFound(f"No pilot district with code {district!r}.")
    bands = [x.strip() for x in age_band.split(",") if x.strip()] if age_band else []
    sexes = [x.strip() for x in sex.split(",") if x.strip()] if sex else []
    if any(b not in AGE_BAND_LABELS for b in bands):
        raise ValidationFailed("Unsupported age band.", [{"field": "age_band", "reason": f"one or more of {list(AGE_BAND_LABELS)}"}])
    if any(x not in SEXES for x in sexes):
        raise ValidationFailed("Unsupported sex.", [{"field": "sex", "reason": f"one or more of {list(SEXES)}"}])
    age_band, sex = ",".join(bands) or None, ",".join(sexes) or None
    return cached(("explorer", district, age_band, sex), GEOGRAPHY_TTL_S, lambda: _explorer(session, district, age_band, sex, sessions, now or _now()))


def _explorer(session: Session, district: str | None, age_band: str | None, sex: str | None, sessions: Callable[[], Session] | None, now: datetime) -> dict[str, Any]:
    core, meta = load_core(session, sessions)
    gmap = {d["code"]: d for d in district_map()["districts"]}
    base = {
        "generated_at": now, "cache_ttl_seconds": GEOGRAPHY_TTL_S, "snapshot": snapshot_meta(meta), "synthetic_banner": SYNTHETIC_BANNER, "sources": SOURCES,
        "filters": {"district": district, "age_band": age_band, "sex": sex},
        "options": {"districts": [{"code": c, "name": d["district"], "state": d["state"]} for c, d in gmap.items()], "age_bands": list(AGE_BAND_LABELS), "sexes": list(SEXES)},
        "not_available": EXPLORER_NOT_AVAILABLE,
        "definitions": {k: DEFINITIONS[k] for k in ("population", "due_residents", "open_care_gaps", "cases_total", "completed", "in_outreach", "gaps_per_100", "closure_rate", "coverage")}
        | {"slice": {"label": "Selected group", "source": "synthetic", "definition": "Residents matching every filter, with the care gap measures of those residents."},
           "pyramid": {"label": "Age and sex", "source": "synthetic", "definition": "Residents, open care gaps and residents due by age band and sex within the selected district (age as of the dataset reference date)."}},
    }  # fmt: skip
    if not core.get("cohorts"):
        return {**base, "available": False, "reason": "The stored snapshot has no age and sex cohorts yet; a refresh is pending or no residents are loaded.", "slice": None, "pyramid": []}
    rows = [{"attended": 0, **dict(zip(COHORT_FIELDS, r, strict=False))} for r in core["cohorts"]]  # old snapshots have no attended count
    in_district = [r for r in rows if not district or r["district"] == district]
    want_bands, want_sexes = set(age_band.split(",")) if age_band else None, set(sex.split(",")) if sex else None
    selected = [r for r in in_district if (want_bands is None or r["age_band"] in want_bands) and (want_sexes is None or r["sex"] in want_sexes)]
    pyramid = []
    for band in AGE_BAND_LABELS:
        for sx in SEXES:
            cell = [r for r in in_district if r["age_band"] == band and r["sex"] == sx]
            if cell:
                pyramid.append({"age_band": band, "sex": sx, "residents": sum(r["residents"] for r in cell), "open_care_gaps": sum(r["open"] for r in cell),
                                "due_residents": sum(r["due"] for r in cell)})  # fmt: skip
    return {**base, "available": True, "as_of": core.get("as_of"), "slice": _slice(selected), "pyramid": pyramid}


# ------------------------------------------------------------------------------------------------ scope
def scope(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    return cached(("scope",), GEOGRAPHY_TTL_S, lambda: _scope(session, now or _now()))


def _scope(session: Session, now: datetime) -> dict[str, Any]:
    gmap = district_map()
    clinics = dict(session.execute(select(Clinic.district_code, func.count()).group_by(Clinic.district_code)).all())
    districts = [{"code": d["code"], "name": d["district"], "state_code": d["state_code"], "state": d["state"], "lat": d["lat"], "lon": d["lon"], "facilities": clinics.get(d["code"], 0)}
                 for d in gmap["districts"]]  # fmt: skip
    pilot_states = {d["state_code"] for d in gmap["districts"]}
    return {
        "generated_at": now,
        "definition": "The places this deployment serves: one country, the states and union territories that contain a pilot district, and the pilot districts (ADR 068). Facilities are the simulated clinics registered for capacity planning.",
        "country": {"code": "IN", "name": "India"},
        "states": [{"code": s["code"], "name": s["name"], "iso_code": s["iso_3166_2"], "type": s["type"], "district_codes": [d["code"] for d in gmap["districts"] if d["state_code"] == s["code"]]}
                   for s in gmap["states"] if s["code"] in pilot_states],  # fmt: skip
        "districts": districts,
        "totals": {"countries": 1, "states_with_coverage": len(pilot_states), "states_listed": len(gmap["states"]), "districts": len(districts), "facilities": sum(clinics.values())},
        "facilities_source": "simulated",
        "not_available": {"households_total": "Residents carry no household attribute; family links are not households."},
    }  # fmt: skip
