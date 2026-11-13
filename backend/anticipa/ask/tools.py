"""Ask Anticipa tools: thin, validated, read-only wrappers over the existing analytics queries (in process, no HTTP,
no free SQL). Every tool returns the same compact shape so charts and tables can be built server side from it:

    {"tool", "args", "definition", "source", "values": {...}, "rows": [{...flat...}], "labels": {field: label},
     "units": {field: "ratio" | "count" | ...}, "notes": [...]}

Aggregates only: there is no tool that returns a resident, a document or a free text field. Groups with fewer than
MIN_CELL cases are dropped and counted in `notes` (small cell suppression)."""
# ruff: noqa: E501

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.orm import Session

from anticipa.analytics import geography as geo
from anticipa.analytics import service as an
from anticipa.analytics.snapshot import PLANNED_OR_LATER, load_core, reached
from anticipa.caregaps.models import CASE_STATUSES, TERMINAL_STATUSES, CareGapCase, CaseStatusHistory
from anticipa.datafabric.models import Resident
from anticipa.engagement.models import CommunicationEvent, ResidentResponse
from anticipa.evaluation.models import EvaluationRun
from anticipa.planning.models import CapacitySlot, Clinic

MIN_CELL = 5  # groups with fewer cases are suppressed
MAX_ROWS = 40
CAPACITY_WINDOW_DAYS = 14
GROUPS = ("district", "programme", "priority", "status", "language", "age_band", "sex")
_MAP = Path(__file__).resolve().parents[3] / "config" / "geography" / "district_map.json"
CACHE_TTL_S = 300.0


class _Args(BaseModel):
    model_config = {"extra": "forbid"}


class NoArgs(_Args):
    pass


class GeographyArgs(_Args):
    level: Literal["district", "state", "country"] = "district"
    code: str | None = Field(None, max_length=40)  # a district code (D01), district name or state name


class ProgrammeArgs(_Args):
    care_activity: str | None = Field(None, max_length=60)
    district: str | None = Field(None, max_length=40)


class BreakdownArgs(_Args):
    group_by: Literal["district", "programme", "priority", "status", "language", "age_band", "sex"]
    district: str | None = Field(None, max_length=40)
    programme: str | None = Field(None, max_length=60)
    priority: str | None = Field(None, max_length=20)
    status: str | None = Field(None, max_length=40)
    language: str | None = Field(None, max_length=8)


class TrendArgs(_Args):
    days: int = Field(30, ge=7, le=90)
    district: str | None = Field(None, max_length=40)
    programme: str | None = Field(None, max_length=60)


class CapacityArgs(_Args):
    district: str | None = Field(None, max_length=40)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str  # what it returns and the definitions the model may quote
    args: type[_Args]
    fn: Callable[[Session, Any, dict[str, str | None]], dict[str, Any]]


# --------------------------------------------------------------------------------------------- shared helpers
@lru_cache(maxsize=1)
def district_map() -> dict[str, dict[str, str]]:
    """code -> {district, state}; empty when the config is absent (codes are then used as labels)."""
    try:
        data = json.loads(_MAP.read_text(encoding="utf-8"))
        return {d["code"]: {"district": d["district"], "state": d["state"]} for d in data["districts"]}
    except (OSError, KeyError, ValueError):
        return {}


def resolve_district(value: str | None) -> str | None:
    """A district code from a code or a (case-insensitive) district name; the input unchanged if unknown."""
    if not value:
        return None
    v = value.strip()
    dm = district_map()
    if v.upper() in dm:
        return v.upper()
    for code, d in dm.items():
        if d["district"].lower() == v.lower():
            return code
    return v


def district_label(code: str | None) -> str:
    d = district_map().get(code or "")
    return f"{d['district']} ({code})" if d else (code or "unknown")


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None else round(float(x), n)


def _result(tool: str, args: dict[str, Any], definition: str, source: str, *, values: dict[str, Any] | None = None,
            rows: list[dict[str, Any]] | None = None, labels: dict[str, str] | None = None,
            units: dict[str, str] | None = None, notes: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    rows = (rows or [])[:MAX_ROWS]
    return {"tool": tool, "args": args, "definition": definition, "source": source, "values": values or {}, "rows": rows,
            "labels": labels or {}, "units": units or {}, "notes": notes or []}  # fmt: skip


def _error(tool: str, args: dict[str, Any], message: str, **extra: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args, "error": message, **extra}


def _age_band() -> Any:
    age = func.date_part("year", func.age(Resident.birth_date))
    return case(
        (Resident.birth_date.is_(None), "unknown"), (age < 40, "under 40"), (age < 50, "40 to 49"),
        (age < 60, "50 to 59"), (age < 70, "60 to 69"), else_="70 and over",
    )  # fmt: skip


def _dims() -> dict[str, Any]:
    return {
        "district": Resident.district_code, "programme": CareGapCase.care_activity, "priority": CareGapCase.priority,
        "status": CareGapCase.status, "language": Resident.preferred_language, "sex": Resident.administrative_sex,
        "age_band": _age_band(),
    }  # fmt: skip


def _filters(district: str | None, programme: str | None, priority: str | None = None, status: str | None = None,
             language: str | None = None, scope: dict[str, str | None] | None = None) -> dict[str, str]:  # fmt: skip
    """The effective filters. The UI scope fills a filter the model left empty or overrides it: the model cannot widen it."""
    scope = scope or {}
    f = {"district": resolve_district(scope.get("district") or district), "programme": scope.get("programme") or programme,
         "priority": priority, "status": status, "language": language}  # fmt: skip
    return {k: v for k, v in f.items() if v}


_COLUMN = {"district": Resident.district_code, "programme": CareGapCase.care_activity, "priority": CareGapCase.priority,
           "status": CareGapCase.status, "language": Resident.preferred_language}  # fmt: skip
LANGUAGES = ("en", "hi", "ar")
PRIORITIES = ("high", "elevated", "routine")


def _conds(f: dict[str, str]) -> list[Any]:
    """SQL conditions for effective filters; status "open" means every status that is not completed, closed or cancelled."""
    return [CareGapCase.status.notin_(TERMINAL_STATUSES) if (k == "status" and v == "open") else _COLUMN[k] == v for k, v in f.items()]


def _core_match(rec: Any, f: dict[str, str]) -> bool:
    for k, v in f.items():
        if k == "status" and v == "open":
            if rec[2] in TERMINAL_STATUSES:
                return False
        elif (rec[_CORE_COL[k]] or "") != v:
            return False
    return True


def check_filters(session: Session, tool: str, args: dict[str, Any], f: dict[str, str]) -> dict[str, Any] | None:
    """An error result listing the valid values when a filter names something that does not exist, so an empty answer
    is never mistaken for "zero cases"."""
    valid: dict[str, Any] = {}
    if "district" in f:
        districts = {*district_map(), *(k for k in load_core(session)[0]["residents"] if k)}  # mapped pilot codes and any code in the data
        if districts and f["district"] not in districts:
            valid["district"] = sorted(districts)
    if "priority" in f and f["priority"] not in PRIORITIES:
        valid["priority"] = list(PRIORITIES)
    if "status" in f and f["status"] not in (*CASE_STATUSES, "open"):
        valid["status"] = ["open", *CASE_STATUSES]
    if "language" in f and f["language"] not in LANGUAGES:
        valid["language"] = list(LANGUAGES)
    if "programme" in f:
        known = sorted({rec[1] for rec in load_core(session)[0]["cases"]})
        if known and f["programme"] not in known:
            valid["programme"] = known
    if not valid:
        return None
    return _error(tool, args, "unknown_filter_value: " + ", ".join(f"{k}={f[k]!r}" for k in valid), valid_values=valid)


_CORE_COL = {"district": 0, "programme": 1, "status": 2, "priority": 3}  # core["cases"] = (district, activity, status, priority, n, planned, outreach)


def _row(code: Any, group_by: str, n: int, open_: int, planned: int, done: int) -> dict[str, Any]:
    code = code if code not in (None, "") else "unknown"
    return {"group": code, "label": district_label(code) if group_by == "district" else code, "cases": n, "open": open_,
            "planned_or_later": planned, "completed": done, "closure_rate": _r(an._ratio(done, n)), "coverage": _r(an._ratio(planned, n))}  # fmt: skip  # noqa: SLF001


def _breakdown(session: Session, group_by: str, f: dict[str, str]) -> tuple[list[dict[str, Any]], int]:
    """Rows per group: cases, open, planned_or_later, completed, closure_rate, coverage. Returns (rows, suppressed).
    Dimensions the analytics snapshot already holds (district, programme, status, priority) come from it, so the
    figures equal the Command Center and the map; language, sex and age band are computed live."""
    live_needed = group_by not in _CORE_COL or any(k not in _CORE_COL for k in f)
    acc: dict[Any, list[int]] = {}
    if not live_needed:
        core, _meta = load_core(session)
        for rec in core["cases"]:
            if not _core_match(rec, f):
                continue
            n, planned = rec[4], rec[5]
            a = acc.setdefault(rec[_CORE_COL[group_by]], [0, 0, 0, 0])
            a[0] += n
            a[1] += n if rec[2] not in TERMINAL_STATUSES else 0
            a[2] += planned
            a[3] += n if rec[2] == "completed" else 0
        raw = [(g, *v) for g, v in acc.items()]
    else:
        dim = _dims()[group_by]
        conds = _conds(f)
        stmt = (
            select(dim.label("g"), func.count(), func.count().filter(CareGapCase.status.notin_(TERMINAL_STATUSES)),
                   func.count().filter(reached(PLANNED_OR_LATER)), func.count().filter(CareGapCase.status == "completed"))  # noqa: SLF001
            .select_from(CareGapCase).join(Resident, Resident.id == CareGapCase.resident_id).where(*conds).group_by(dim)
        )  # fmt: skip
        raw = [tuple(r) for r in session.execute(stmt).all()]
    rows, suppressed = [], 0
    for g, n, open_, planned, done in raw:
        if n < MIN_CELL:
            suppressed += 1
            continue
        rows.append(_row(g, group_by, n, open_, planned, done))
    rows.sort(key=lambda r: (-r["cases"], str(r["group"])))
    return rows, suppressed


BREAKDOWN_LABELS = {"label": "Group", "cases": "Cases opened", "open": "Open cases", "planned_or_later": "Planned or later",
                    "completed": "Completed", "closure_rate": "Closure rate", "coverage": "Planning coverage"}  # fmt: skip
BREAKDOWN_UNITS = {"cases": "count", "open": "count", "planned_or_later": "count", "completed": "count",
                   "closure_rate": "ratio", "coverage": "ratio"}  # fmt: skip
BREAKDOWN_DEF = (
    "Per group: cases = care gap cases opened; open = not completed, closed not completed or cancelled; planned_or_later = "
    "reached planning (a simulated slot was proposed) or beyond; closure_rate = completed / cases; coverage = planned_or_later / cases. "
    f"Groups with fewer than {MIN_CELL} cases are suppressed."
)
PROGRAMME_NOTE = "Programme keys are care activity codes such as breast_cancer_screening."


def _cached(key: tuple, compute: Callable[[], Any]) -> Any:
    return an.cached(("ask", *key), CACHE_TTL_S, compute)


def _breakdown_result(tool: str, session: Session, group_by: str, f: dict[str, str], args: dict[str, Any]) -> dict[str, Any]:
    rows, suppressed = _cached(("bd", group_by, tuple(sorted(f.items()))), lambda: _breakdown(session, group_by, f))
    total = sum(r["cases"] for r in rows)
    done = sum(r["completed"] for r in rows)
    notes = [f"{suppressed} group(s) with fewer than {MIN_CELL} cases suppressed."] if suppressed else []
    if not rows:
        notes.append("No care gap cases match these filters.")
    return _result(tool, args, BREAKDOWN_DEF, "simulated", values={"group_by": group_by, "groups": len(rows), "cases_in_groups": total,
                   "completed_in_groups": done, "closure_rate_overall": _r(an._ratio(done, total))},  # noqa: SLF001
                   rows=rows, labels=BREAKDOWN_LABELS, units=BREAKDOWN_UNITS, notes=notes)  # fmt: skip


# --------------------------------------------------------------------------------------------- tools
def get_overview_kpis(session: Session, a: NoArgs, scope: dict[str, str | None]) -> dict[str, Any]:
    cc = an.command_center(session, limited=True, internal=False, trend_days=30)
    rows = [{"key": k["key"], "label": k["label"], "value": k["value"], "definition": k["definition"]} for k in cc["kpis"]]
    prio = {f"{p['priority']}_{f}": p[f] for p in cc["gaps_by_priority"]["items"] for f in ("open", "completed", "total")}
    alerts = [{"title": r["title"], "message": r["message"]} for r in cc["alerts"]]
    return _result("get_overview_kpis", {}, "Headline counts and rates; each row carries its own definition.", "simulated",
                   values={**prio, "alerts_active": len(alerts)}, rows=rows, labels={"label": "Measure", "value": "Value"},
                   units={"value": "mixed"}, notes=[f"Active alert: {x['title']}: {x['message']}" for x in alerts])  # fmt: skip


def get_funnel(session: Session, a: NoArgs, scope: dict[str, str | None]) -> dict[str, Any]:
    f = an.funnel(session)
    rows = [{"key": s["key"], "label": s["label"], "residents": s["count"], "share_of_residents": _r(s["share_of_residents"]),
             "conversion_from_previous": _r(s.get("conversion_from_previous"))} for s in f["stages"]]  # fmt: skip
    leak = {f"leakage_{i['status']}": i["cases"] for i in f["leakage"]["items"]}
    return _result("get_funnel", {}, f["note"], "simulated", values=leak, rows=rows,
                   labels={"label": "Stage", "residents": "Residents", "share_of_residents": "Share of residents",
                           "conversion_from_previous": "Conversion from previous stage"},
                   units={"residents": "count", "share_of_residents": "ratio", "conversion_from_previous": "ratio"})  # fmt: skip


def get_care_gap_breakdown(session: Session, a: BreakdownArgs, scope: dict[str, str | None]) -> dict[str, Any]:
    f = _filters(a.district, a.programme, a.priority, a.status, a.language, scope)
    if (bad := check_filters(session, "get_care_gap_breakdown", a.model_dump(exclude_none=True), f)) is not None:
        return bad
    return _breakdown_result("get_care_gap_breakdown", session, a.group_by, f, a.model_dump(exclude_none=True))


def get_programme_stats(session: Session, a: ProgrammeArgs, scope: dict[str, str | None]) -> dict[str, Any]:
    f = _filters(a.district, a.care_activity, scope=scope)
    if (bad := check_filters(session, "get_programme_stats", a.model_dump(exclude_none=True), f)) is not None:
        return bad
    if "district" in f:  # a district slice: the case grid (the programmes service is whole population)
        out = _breakdown_result("get_programme_stats", session, "programme", f, a.model_dump(exclude_none=True))
        out["notes"].append(PROGRAMME_NOTE)
        return out
    p = geo.programmes(session)
    rows = [{"group": r["care_activity"], "label": r["care_activity"], "cases": r["cases_total"], "open": r["open_care_gaps"], "completed": r["completed"],
             "in_outreach": r["in_outreach"], "due_residents": r["due_residents"], "closure_rate": r["closure_rate"], "coverage": r["coverage"]}
            for r in p["programmes"] if not f.get("programme") or r["care_activity"] == f["programme"]]  # fmt: skip
    defs = " ".join(f"{k}: {v['definition']}" for k, v in p["definitions"].items() if k in ("closure_rate", "coverage", "due_residents", "open_care_gaps"))
    return _result("get_programme_stats", a.model_dump(exclude_none=True), defs or BREAKDOWN_DEF, "simulated", values={"programmes": len(rows), "population": p["population"]},
                   rows=rows, labels={**BREAKDOWN_LABELS, "due_residents": "Residents due", "in_outreach": "In outreach"},
                   units={**BREAKDOWN_UNITS, "due_residents": "count", "in_outreach": "count"}, notes=[PROGRAMME_NOTE])  # fmt: skip


def _area_row(x: dict[str, Any]) -> dict[str, Any]:
    return {"group": x["code"], "label": district_label(x["code"]) if x["level"] == "district" else x["name"], "district": x.get("name"), "state": x.get("state", x["name"]),
            "residents": x["population"], "cases": x["cases_total"], "open": x["open_care_gaps"], "completed": x["completed"], "in_outreach": x["in_outreach"],
            "due_residents": x["due_residents"], "gaps_per_100": x["gaps_per_100"], "closure_rate": x["closure_rate"], "coverage": x["coverage"]}  # fmt: skip


def get_geography(session: Session, a: GeographyArgs, scope: dict[str, str | None]) -> dict[str, Any]:
    """Districts, states or the country from the analytics geography service (the one behind the India map)."""
    f = _filters(None, None, scope=scope)
    if (bad := check_filters(session, "get_geography", a.model_dump(exclude_none=True), f)) is not None:
        return bad
    notes = ["District codes D01 to D10 are placed in real districts by the synthetic data design; this says nothing about real people."]
    country = geo.geography(session, level="country")
    states = [x for x in country["areas"] if x["coverage_status"] == "pilot"]
    if "programme" in f:  # a programme slice: the case grid (the geography service is all programmes)
        rows_d, suppressed = _cached(("geo", tuple(sorted(f.items()))), lambda: _breakdown(session, "district", f))
        residents = {(k or "unknown"): v for k, v in load_core(session)[0]["residents"].items()}
        rows = []
        for r in rows_d:
            d = district_map().get(r["group"], {})
            rows.append({**r, "district": d.get("district", r["group"]), "state": d.get("state", "unknown"), "residents": residents.get(r["group"], 0)})
        if suppressed:
            notes.append(f"{suppressed} district(s) with fewer than {MIN_CELL} cases suppressed.")
    else:
        if a.level == "country":
            rows = [_area_row({**country["area"], "level": "country"})]
        elif a.level == "state":
            rows = [_area_row(x) for x in states]
        else:
            rows = [_area_row(d) for st in states for d in geo.geography(session, level="state", code=st["code"])["areas"]]
        if "district" in f:
            rows = [r for r in rows if r["group"] == f["district"]]
    if a.code:
        want = a.code.strip().lower()
        rows = [r for r in rows if want in (str(r["group"]).lower(), str(r["label"]).lower(), str(r.get("district", "")).lower(), str(r.get("state", "")).lower())]
    rows.sort(key=lambda r: (-r["cases"], str(r["group"])))
    labels = {**BREAKDOWN_LABELS, "residents": "Residents", "state": "State", "due_residents": "Residents due", "gaps_per_100": "Open gaps per 100 residents", "in_outreach": "In outreach"}
    units = {**BREAKDOWN_UNITS, "residents": "count", "due_residents": "count", "gaps_per_100": "mixed", "in_outreach": "count"}
    return _result("get_geography", a.model_dump(exclude_none=True), BREAKDOWN_DEF + " residents = residents placed in the area; gaps_per_100 = open care gaps per 100 residents.", "simulated",
                   values={"level": a.level, "areas": len(rows)}, rows=rows, labels=labels, units=units, notes=notes)  # fmt: skip


def get_closure_trend(session: Session, a: TrendArgs, scope: dict[str, str | None]) -> dict[str, Any]:
    now = datetime.now(UTC)
    start = (now - timedelta(days=a.days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    f = _filters(a.district, a.programme, scope=scope)
    if (bad := check_filters(session, "get_closure_trend", a.model_dump(exclude_none=True), f)) is not None:
        return bad
    conds = _conds(f)

    def compute() -> list[tuple[Any, int, int]]:
        day = cast(func.timezone("UTC", CaseStatusHistory.occurred_at), Date)
        stmt = (
            select(day, func.count().filter(CaseStatusHistory.to_status == "completed"), func.count().filter(CaseStatusHistory.from_status.is_(None)))
            .select_from(CaseStatusHistory).join(CareGapCase, CareGapCase.id == CaseStatusHistory.case_id)
            .join(Resident, Resident.id == CareGapCase.resident_id)
            .where(CaseStatusHistory.occurred_at >= start, *conds).group_by(day).order_by(day)
        )  # fmt: skip
        return [(d, c, o) for d, c, o in session.execute(stmt).all()]

    data = _cached(("trend", a.days, tuple(sorted(f.items())), start.date().isoformat()), compute)
    by_day = {d: (c, o) for d, c, o in data}
    rows = [{"date": (start.date() + timedelta(days=i)).isoformat(), "closed": by_day.get(start.date() + timedelta(days=i), (0, 0))[0],
             "opened": by_day.get(start.date() + timedelta(days=i), (0, 0))[1]} for i in range(a.days)] if by_day else []  # fmt: skip
    return _result("get_closure_trend", a.model_dump(exclude_none=True),
                   f"Per UTC day over the last {a.days} days: closed = status changes to completed; opened = cases created.", "simulated",
                   values={"days": a.days, "total_opened": sum(r["opened"] for r in rows), "total_closed": sum(r["closed"] for r in rows)},
                   rows=rows, labels={"date": "Date", "closed": "Closed", "opened": "Opened"}, units={"closed": "count", "opened": "count"},
                   notes=[] if rows else ["No case history in this window."])  # fmt: skip


def get_capacity_overview(session: Session, a: CapacityArgs, scope: dict[str, str | None]) -> dict[str, Any]:
    now = datetime.now(UTC)
    end = now + timedelta(days=CAPACITY_WINDOW_DAYS)
    district = resolve_district(scope.get("district") or a.district)

    def compute() -> list[tuple[Any, Any, Any]]:
        stmt = (
            select(Clinic.district_code, func.coalesce(func.sum(CapacitySlot.available_capacity), 0), func.coalesce(func.sum(CapacitySlot.reserved_capacity), 0))
            .select_from(CapacitySlot).join(Clinic, Clinic.id == CapacitySlot.clinic_id)
            .where(CapacitySlot.slot_status == "open", CapacitySlot.start_time >= now, CapacitySlot.start_time < end).group_by(Clinic.district_code)
        )  # fmt: skip
        return list(session.execute(stmt).all())

    data = _cached(("cap", now.strftime("%Y%m%d%H")), compute)
    rows = [{"group": d, "label": district_label(d), "seats": int(s), "reserved": int(r), "utilisation": _r(an._ratio(int(r), int(s))), "free": int(s) - int(r)}  # noqa: SLF001
            for d, s, r in data if not district or d == district]  # fmt: skip
    rows.sort(key=lambda x: str(x["group"]))
    seats, reserved = sum(x["seats"] for x in rows), sum(x["reserved"] for x in rows)
    return _result("get_capacity_overview", a.model_dump(exclude_none=True),
                   f"Simulated clinic seats over open capacity slots starting in the next {CAPACITY_WINDOW_DAYS} days: seats = available capacity; reserved = confirmed seats; utilisation = reserved / seats.",
                   "simulated", values={"window_days": CAPACITY_WINDOW_DAYS, "seats": seats, "reserved": reserved, "utilisation": _r(an._ratio(reserved, seats))},  # noqa: SLF001
                   rows=rows, labels={"label": "District", "seats": "Seats", "reserved": "Reserved", "free": "Free", "utilisation": "Utilisation"},
                   units={"seats": "count", "reserved": "count", "free": "count", "utilisation": "ratio"},
                   notes=[] if rows else ["No simulated capacity is registered in this window."])  # fmt: skip


def get_engagement_overview(session: Session, a: NoArgs, scope: dict[str, str | None]) -> dict[str, Any]:
    def count_by(col: Any, dim: str) -> list[dict[str, Any]]:
        return [{"dimension": dim, "group": g or "unknown", "count": n} for g, n in session.execute(select(col, func.count()).group_by(col)).all()]

    rows = (count_by(CommunicationEvent.delivery_state, "delivery_state") + count_by(CommunicationEvent.channel, "channel")
            + count_by(CommunicationEvent.language, "language") + count_by(ResidentResponse.response_type, "response_type"))  # fmt: skip
    total = sum(r["count"] for r in rows if r["dimension"] == "delivery_state")
    delivered = sum(r["count"] for r in rows if r["dimension"] == "delivery_state" and r["group"] == "delivered")
    responses = sum(r["count"] for r in rows if r["dimension"] == "response_type")
    return _result("get_engagement_overview", {}, "Simulated outreach messages by delivery state, channel and language, and simulated resident responses by type. Nothing reached a real person.",
                   "simulated", values={"messages_total": total, "delivered_share": _r(an._ratio(delivered, total)), "responses_total": responses},  # noqa: SLF001
                   rows=rows, labels={"dimension": "Dimension", "group": "Group", "count": "Count"}, units={"count": "count"})  # fmt: skip


def get_evaluation_summary(session: Session, a: NoArgs, scope: dict[str, str | None]) -> dict[str, Any]:
    final_first = (EvaluationRun.run_type == "test_final").desc()
    run = session.execute(select(EvaluationRun).where(EvaluationRun.status == "complete").order_by(final_first, EvaluationRun.created_at.desc()).limit(1)).scalar_one_or_none()
    if run is None:
        return _result("get_evaluation_summary", {}, "No evaluation run exists yet.", "evaluation", notes=["No evaluation run exists yet."])
    rows = []
    for mid, m in (run.metrics or {}).items():
        if isinstance(m, dict) and m.get("status") == "measured" and isinstance(m.get("value"), (int, float)):
            rows.append({"metric_id": mid, "label": str(m.get("metric", mid))[:80], "value": _r(m["value"]), "unit": m.get("unit"), "n": m.get("n")})
    rows = rows[:25]
    gs = run.gate_summary or {}
    return _result("get_evaluation_summary", {}, "Aggregate results of the latest complete evaluation run against planted ground truth on generated data. A software evaluation, not clinical validation.",
                   "evaluation", values={"run_type": run.run_type, "split": run.split, "dataset_version": run.dataset_version, **{f"gates_{k}": v for k, v in gs.items()}},
                   rows=rows, labels={"label": "Metric", "value": "Value", "n": "Sample size"}, units={"value": "mixed", "n": "count"})  # fmt: skip


TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("get_overview_kpis", "Headline KPIs: residents, residents with open gaps, open and total care gaps, gaps closed, closure rate, in outreach, plus gaps by priority and active alerts. Args: none.", NoArgs, get_overview_kpis),
    Tool("get_funnel", "Care gap funnel stages (residents, evaluated, due, case opened, planned, in outreach, closed) with shares and stage to stage conversion, plus leakage counts. Args: none.", NoArgs, get_funnel),
    Tool("get_geography", "Per district, per state or for the whole country: residents, cases, open cases, completed, residents due, open gaps per 100 residents, closure rate and planning coverage (the figures behind the India map). Args: level (district|state|country, default district), code (optional: district code like D02, district name or state name).", GeographyArgs, get_geography),
    Tool("get_programme_stats", "Per programme (care activity) cases, open, planned, completed, closure rate and coverage. Args: care_activity (optional programme key), district (optional code or name).", ProgrammeArgs, get_programme_stats),
    Tool("get_care_gap_breakdown", "Care gap counts and rates grouped by one dimension. Args: group_by (district|programme|priority|status|language|age_band|sex), optional filters district, programme, priority (high|elevated|routine), status (a case status, or open for every case not completed, closed or cancelled), language (en|hi|ar).", BreakdownArgs, get_care_gap_breakdown),
    Tool("get_closure_trend", "Daily cases opened and closed over a window. Args: days (7 to 90, default 30), optional district, programme.", TrendArgs, get_closure_trend),
    Tool("get_capacity_overview", f"Simulated clinic seats, reserved seats and utilisation per district over the next {CAPACITY_WINDOW_DAYS} days. Args: district (optional).", CapacityArgs, get_capacity_overview),
    Tool("get_engagement_overview", "Simulated outreach messages by delivery state, channel and language, and simulated responses by type. Args: none.", NoArgs, get_engagement_overview),
    Tool("get_evaluation_summary", "Latest evaluation run: gate counts (pass, fail, warn, not evaluated) and measured metrics on planted ground truth. Args: none.", NoArgs, get_evaluation_summary),
)}  # fmt: skip


def run_tool(session: Session, name: str, args_json: str, scope: dict[str, str | None], call_id: str) -> dict[str, Any]:
    """Validate and execute one tool call. Never raises: bad input comes back as an `error` result the model can read."""
    tool = TOOLS.get(name)
    if tool is None:
        return {**_error(name, {}, "unknown_tool", valid_tools=sorted(TOOLS)), "call_id": call_id}
    try:
        raw = json.loads(args_json or "{}")
        if not isinstance(raw, dict):
            raise ValueError("arguments must be a JSON object")
        args = tool.args.model_validate(raw)
    except (ValueError, ValidationError) as e:
        msg = e.errors()[0]["msg"] if isinstance(e, ValidationError) and e.errors() else str(e)
        return {**_error(name, {}, f"invalid_arguments: {msg}"[:200], schema=tool.args.model_json_schema().get("properties", {})), "call_id": call_id}
    out = tool.fn(session, args, scope)
    out["call_id"] = call_id
    return out


def tool_catalogue() -> str:
    """Deterministic text for the static (prompt cached) system prompt."""
    return "\n".join(f"- {t.name}: {t.description}" for t in TOOLS.values())
