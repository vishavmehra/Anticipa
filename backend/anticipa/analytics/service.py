"""Read-only analytics for the Command Center and the judge-facing screens: command-center KPIs, the care gap
funnel and the live processing monitor. Every value is computed from real tables / files at request time and
carries a `definition` and a `source` label. Nothing here writes: no session.add, no commit, no audit event.

Source labels (the least-real input wins when a metric mixes inputs):
  live        system state at request time (jobs, AI spend, pipeline files, audit trail)
  synthetic   derived from synthetic (generated) residents, records and documents
  simulated   simulated planning / outreach / closure data; nothing reached a real person
  evaluation  from the evaluation harness (not used by these endpoints; kept in the legend for the UI)

Heavy aggregates are cached per process for a few seconds (`cached`). ponytail: per-process dict; use a shared
cache (and single-flight across workers) if the API scales out.
"""
# ruff: noqa: E501

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from anticipa.ai.models import ModelInvocation
from anticipa.ai.settings import get_ai_settings
from anticipa.analytics.places import programme_label, resolve_scope, scope_info
from anticipa.analytics.snapshot import COMPARE_DAYS, OUTREACH_STATUSES, RECENT_CHANGES, load_core
from anticipa.caregaps.models import TERMINAL_STATUSES, CareGapCase
from anticipa.core.errors import ValidationFailed
from anticipa.core.models import AuditEvent
from anticipa.datafabric import synthea
from anticipa.datafabric.models import Dataset, Document, Resident
from anticipa.evidence.models import EvidenceFact
from anticipa.jobs.models import ProcessingJob
from anticipa.planning.models import CapacitySlot, Clinic

SYNTHETIC_BANNER = "Demonstration data only: every record is generated or simulated, and no real person is represented. Nothing here is clinical validation."
SOURCES = {
    "live": "System state at request time (jobs, AI spend, pipeline files, audit trail).",
    "synthetic": "Derived from generated residents, records and documents; no real person is represented.",
    "simulated": "Simulated planning, outreach or closure data; nothing reached a real person.",
    "evaluation": "From the evaluation harness against held out ground truth.",
}

# --- cache TTLs (seconds). Tests set these to 0 to bypass the cache.
COMMAND_CENTER_TTL_S = 30.0  # cold: 1.5 s p50 on 116k cases + 1.2M evaluations (0.4 s limited)
FUNNEL_TTL_S = 60.0  # cold: 2.8 s p50 on the same data (two scans of the 1.2M evaluations)
PROCESSING_TTL_S = 10.0
ALERTS_TTL_S = 15.0
DEMAND_TTL_S = 30.0
PLANNABLE = ("approved_for_planning", "awaiting_capacity")  # approved cases waiting for a slot

# --- alert thresholds: explicit and documented; returned verbatim in `alert_rules`.
CAPACITY_WINDOW_DAYS = 7
CAPACITY_UTILISATION_ALERT = 0.90  # >= warning; >= 1.0 critical (fully booked)
OUTREACH_BACKLOG_DAYS = 7  # a planned case with no outreach for more than this many days
EVIDENCE_REVIEW_SHARE_ALERT = 0.10  # share of evidence facts awaiting human review
EVIDENCE_REVIEW_MIN_FACTS = 20  # below this many facts the share is not meaningful (no alert)
FAILED_JOBS_WINDOW_HOURS = 24
RECENT_ACTIVITY_LIMIT = 10
ACTIVE_WINDOW_S = 1800  # a pipeline is "active" if its manifest changed within this window
THROUGHPUT_BATCHES = 20  # recent completed generation batches used for the throughput estimate

PRIORITY_ORDER = ("high", "elevated", "routine")


# ------------------------------------------------------------------------------------------------ helpers
_CACHE: dict[tuple, tuple[float, Any]] = {}
_LOCKS: dict[tuple, threading.Lock] = {}
_GUARD = threading.Lock()


def cached(key: tuple, ttl: float, compute: Callable[[], Any]) -> Any:
    """Return the cached value for `key` if younger than `ttl`; otherwise compute it once (concurrent callers wait)."""
    if ttl <= 0:
        return compute()
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = compute()
        _CACHE[key] = (time.monotonic(), value)
        return value


def clear_cache() -> None:
    _CACHE.clear()


def _now() -> datetime:
    return datetime.now(UTC)


def _ratio(num: float, den: float, digits: int = 4) -> float | None:
    return round(num / den, digits) if den else None


def _f(value: Any) -> float:
    return float(value or 0)


def metric(key: str, label: str, value: Any, definition: str, source: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "value": value, "definition": definition, "source": source, **extra}


# ------------------------------------------------------------------------------------------------ rollups over the core snapshot
def scoped_core(core: dict[str, Any], districts: tuple[str, ...] | None) -> dict[str, Any]:
    """The core restricted to some districts (None keeps all) in the shape the shaping code expects: `trend`, status counts
    per comparison period (`statuses`), `recent_changes` and `evidence` are rebuilt from their per district parts.
    A snapshot that predates the per district parts keeps the global values when unscoped and has none when scoped."""
    keep = (lambda d: True) if districts is None else set(districts).__contains__
    out = dict(core)
    for key in ("residents", "stages", "evaluated", "due"):
        out[key] = {d: v for d, v in core[key].items() if keep(d)}
    out["cases"] = [r for r in core["cases"] if keep(r[0])]
    out["due_by_activity"] = [r for r in core["due_by_activity"] if keep(r[0])]
    out["cohorts"] = [r for r in core.get("cohorts", []) if keep(r[0])]
    by_day = core.get("trend_by_district")
    trend: dict[str, list[int]] = {}
    if by_day is None:
        trend = dict(core.get("trend", {})) if districts is None else {}
    else:
        for d, days in by_day.items():
            for day, (closed, opened) in days.items():
                if keep(d):
                    acc = trend.setdefault(day, [0, 0])
                    acc[0], acc[1] = acc[0] + closed, acc[1] + opened
    out["trend"] = trend
    stocks: dict[str, Any] = {}
    for days, stock in (core.get("stocks") or {}).items():
        by = stock.get("by_district")
        if by is not None:
            statuses: dict[str, int] = {}
            for d, counts in by.items():
                if keep(d):
                    for status, n in counts.items():
                        statuses[status] = statuses.get(status, 0) + n
            stocks[days] = {"as_of": stock["as_of"], "statuses": statuses, "closed_residents": sum(n for d, n in (stock.get("closed_residents") or {}).items() if keep(d))}
        elif districts is None and "statuses" in stock:
            stocks[days] = stock
    out["stocks"] = stocks
    previous: dict[str, Any] = {}
    for days, snap in (core.get("funnel_previous") or {}).items():
        agg: dict[str, Any] = {"opened": [0, 0], "planned": [0, 0], "outreach": [0, 0], "evaluated": 0}
        for d, stages in snap["by_district"].items():
            if keep(d):
                for stage in ("opened", "planned", "outreach"):
                    agg[stage][0] += stages.get(stage, [0, 0])[0]
                    agg[stage][1] += stages.get(stage, [0, 0])[1]
                agg["evaluated"] += stages.get("evaluated", 0)
        previous[days] = {"as_of": snap["as_of"], **agg}
    out["funnel_previous"] = previous
    recent_by = core.get("recent_by_district")
    if recent_by is None:
        out["recent_changes"] = list(core.get("recent_changes", [])) if districts is None else []
    else:
        merged = [c for d, rows in recent_by.items() if keep(d) for c in rows]
        out["recent_changes"] = sorted(merged, key=lambda c: c["at"], reverse=True)[:RECENT_CHANGES]
    evidence_by = core.get("evidence_by_district")
    if evidence_by is not None:
        out["evidence"] = {"total": sum(v[0] for d, v in evidence_by.items() if keep(d)), "pending": sum(v[1] for d, v in evidence_by.items() if keep(d))}
    elif districts is not None:
        out["evidence"] = {"total": 0, "pending": 0}
    return out


def timings_block(core: dict[str, Any], district: str | None, state: str | None) -> dict[str, Any]:
    """Median and 90th percentile hours from first planned to attended and to completed, exact for the whole population, a
    district or a state (medians cannot be combined, so each scope is computed in the snapshot)."""
    t = core.get("timings") or {}
    if district:
        found = (t.get("district") or {}).get(resolve_scope(district, None)[0])
    elif state:
        found = (t.get("state") or {}).get(scope_info(None, state)["state"])
    else:
        found = t.get("all")

    def part(key: str) -> dict[str, Any]:
        n, median, p90 = (found or {}).get(key) or [0, None, None]
        return {
            "cases": n, "median_hours": median, "p90_hours": p90,
            "median_days": None if median is None else round(median / 24, 4), "p90_days": None if p90 is None else round(p90 / 24, 4),
        }

    return {
        "definition": "Hours from a case first being planned to first attended (the case reaches awaiting completion evidence) and to completed. Median and 90th percentile over cases that got that far; null when none did. Planning, attendance and closure are simulated, so the timestamps are simulated too.",
        "source": "simulated", "time_to_attend": part("attended"), "time_to_close": part("closed"),
    }  # fmt: skip


def rollup(core: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per district totals (and per programme within it) from the core grid. District "" means no district recorded."""
    zero = {"cases": 0, "open": 0, "completed": 0, "planned": 0, "in_outreach": 0, "due": 0, "up_to_date": 0}
    out: dict[str, dict[str, Any]] = {}

    def row(district: str) -> dict[str, Any]:
        return out.setdefault(district, {"residents": 0, "evaluated": 0, "stage": {"opened": 0, "planned": 0, "outreach": 0, "closed": 0, "open_gaps": 0, "plannable": 0}, **zero, "programmes": {}})

    for district, n in core["residents"].items():
        row(district)["residents"] = n
    for district, n in core["evaluated"].items():
        row(district)["evaluated"] = n
    for district, stage in core["stages"].items():
        row(district)["stage"] = stage
    for district, n in core["due"].items():
        row(district)["due"] = n
    for district, activity, status, _prio, n, planned, _outreach in core["cases"]:
        r = row(district)
        for target in (r, r["programmes"].setdefault(activity, dict(zero))):
            target["cases"] += n
            target["open"] += n if status not in TERMINAL_STATUSES else 0
            target["completed"] += n if status == "completed" else 0
            target["planned"] += planned
            target["in_outreach"] += n if status in OUTREACH_STATUSES else 0
    for district, activity, n, *done in core["due_by_activity"]:  # old snapshots have no up to date count
        programme = row(district)["programmes"].setdefault(activity, dict(zero))
        programme["due"], programme["up_to_date"] = n, (done[0] if done else 0)
    return out


def area_metrics(r: dict[str, Any]) -> dict[str, Any]:
    """The headline measures of one area (district, state or country) from summed rollup rows."""
    return {
        "population": r["residents"], "due_residents": r["due"], "open_care_gaps": r["open"],
        "residents_with_open_gaps": r["stage"]["open_gaps"], "cases_total": r["cases"], "completed": r["completed"],
        "in_outreach": r["in_outreach"], "gaps_per_100": _ratio(r["open"] * 100, r["residents"], 2),
        "closure_rate": _ratio(r["completed"], r["cases"]), "coverage": _ratio(r["planned"], r["cases"]),
    }  # fmt: skip


def snapshot_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": meta["source"], "computed_at": meta["computed_at"], "age_seconds": meta["age_seconds"],
        "definition": "Aggregates are precomputed and refreshed after analytics relevant jobs, on the command line and when the data changes. The source 'live' means no stored snapshot existed and the figures were computed for this request.",
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ command center
def command_center(
    session: Session, *, limited: bool = False, internal: bool = False, trend_days: int = 30, compare: bool = False,
    district: str | None = None, state: str | None = None, sessions: Callable[[], Session] | None = None, now: datetime | None = None,
) -> dict[str, Any]:  # fmt: skip
    """`internal` (system.configure holders only) adds audit events, background jobs and the failed jobs alert; product
    users and judges see product data only (owner decision 2026-11-12)."""
    if compare and trend_days not in COMPARE_DAYS:
        raise ValidationFailed("Unsupported comparison period.", [{"field": "days", "reason": f"with compare=previous, one of {list(COMPARE_DAYS)}"}])
    scope = scope_info(district, state)  # validates the codes before anything is cached or computed
    key = ("command-center", limited, internal, trend_days, compare, scope["district"], scope["state"])
    return cached(key, COMMAND_CENTER_TTL_S, lambda: _command_center(session, sessions, limited, internal, trend_days, compare, scope, now or _now()))


def _command_center(
    session: Session, sessions: Callable[[], Session] | None, limited: bool, internal: bool, trend_days: int, compare: bool,
    scope: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    full, meta = load_core(session, sessions)
    districts = None if scope["districts"] is None else tuple(scope["districts"])
    core = scoped_core(full, districts)
    roll = rollup(core)
    kpis = _kpis_from_core(core)
    priority: dict[str, dict[str, int]] = {p: {"open": 0, "completed": 0, "total": 0} for p in PRIORITY_ORDER}
    for _d, _a, status, prio, n, _pl, _ou in core["cases"]:
        row = priority.setdefault(prio, {"open": 0, "completed": 0, "total": 0})
        row["total"] += n
        row["open"] += n if status not in TERMINAL_STATUSES else 0
        row["completed"] += n if status == "completed" else 0
    out: dict[str, Any] = {
        "generated_at": now,
        "cache_ttl_seconds": COMMAND_CENTER_TTL_S,
        "snapshot": snapshot_meta(meta),
        "limited": limited,
        "restricted": ["equity", "recent_activity"] if limited else [],
        "synthetic_banner": SYNTHETIC_BANNER,
        "sources": SOURCES,
        "dataset": {"latest_version": core["latest_dataset"]},
        "scope": scope,
        "kpis": kpis,
        "gaps_by_priority": {
            "definition": "Care gap cases grouped by case priority (from the guideline version); open = not in a terminal status.",
            "source": "synthetic",
            "items": [{"priority": p, **priority[p]} for p in (*PRIORITY_ORDER, *sorted(set(priority) - set(PRIORITY_ORDER)))],
        },
        "closure_trend": _closure_trend(core, trend_days, now),
        "timings": timings_block(full, scope["district"], scope["state"]),
        "equity": None,
        "recent_activity": None,
    }
    if not limited:
        out["equity"] = _equity(roll)
        out["recent_activity"] = _recent_activity(session, core, internal and districts is None)  # audit and jobs are not per district
    alert_rules = _alert_rules(session, core, now, internal and districts is None, districts)
    out["alert_rules"] = alert_rules
    out["alerts"] = [r for r in alert_rules if r["status"] == "alert"]
    if compare:
        out["comparison"] = _comparison(core, trend_days, now)
    return out


def _comparison(core: dict[str, Any], days: int, now: datetime) -> dict[str, Any]:
    """Period on period change: each KPI now versus the same KPI `days` days ago (rebuilt from the append only case status
    history), and the cases opened and closed in the last `days` days versus the `days` before that."""
    stock = (core.get("stocks") or {}).get(str(days))
    base: dict[str, Any] = {
        "window_days": days, "source": "simulated",
        "definition": f"Change since {days} days ago. Counts of cases are rebuilt from the append only status history as of that moment; rates are shown as percentage point change. Flows compare the last {days} days with the {days} days before them.",
        "not_available": {
            "residents_total": "Residents are loaded in bulk and no history of the population over time is kept.",
            "residents_with_open_gaps": "Resident level counts cannot be rebuilt for a past date from the status history.",
        },
    }  # fmt: skip
    if stock is None:
        return {**base, "available": False, "reason": "The stored snapshot predates period comparison; a refresh is pending."}
    statuses = stock["statuses"]
    prev = {
        "care_gaps_total": sum(statuses.values()),
        "care_gaps_open": sum(n for st, n in statuses.items() if st not in TERMINAL_STATUSES),
        "gaps_closed": statuses.get("completed", 0),
        "in_outreach": sum(statuses.get(st, 0) for st in OUTREACH_STATUSES),
    }
    prev["closure_rate"] = _ratio(prev["gaps_closed"], prev["care_gaps_total"])
    current = {k["key"]: k["value"] for k in _kpis_from_core(core)}
    kpis: dict[str, Any] = {}
    for key in ("care_gaps_total", "care_gaps_open", "gaps_closed", "in_outreach", "closure_rate"):
        cur, old = current[key], prev[key]
        rate = key == "closure_rate"
        delta = None if cur is None or old is None else round(cur - old, 4) if rate else cur - old
        kpis[key] = {"current": cur, "previous": old, "delta": delta, "delta_pp": None if delta is None or not rate else round(delta * 100, 1)}
    today = now.date()

    def window(offset: int, index: int) -> int:
        lo, hi = today - timedelta(days=offset + days - 1), today - timedelta(days=offset)
        return sum(v[index] for d, v in core["trend"].items() if lo <= date.fromisoformat(d) <= hi)

    flows: dict[str, Any] = {}
    for name, index in (("closed", 0), ("opened", 1)):
        cur, old = window(0, index), window(days, index)
        flows[name] = {"current": cur, "previous": old, "delta": cur - old, "delta_pct": _ratio((cur - old) * 100, old, 1)}
    return {**base, "available": True, "previous_as_of": stock["as_of"], "kpis": kpis, "flows": flows}


def alerts_summary(session: Session, *, internal: bool = False, sessions: Callable[[], Session] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The alert count for a badge: the same rules as the Command Center, without the rest of its payload."""
    return cached(("alerts-summary", internal), ALERTS_TTL_S, lambda: _alerts_summary(session, sessions, internal, now or _now()))


def _alerts_summary(session: Session, sessions: Callable[[], Session] | None, internal: bool, now: datetime) -> dict[str, Any]:
    core, meta = load_core(session, sessions)
    active = [r for r in _alert_rules(session, core, now, internal) if r["status"] == "alert"]
    return {
        "generated_at": now, "snapshot": snapshot_meta(meta), "alerts_active": len(active),
        "by_severity": {sev: sum(1 for r in active if r["severity"] == sev) for sev in ("critical", "warning")},
        "items": [{"key": r["key"], "title": r["title"], "severity": r["severity"], "message": r["message"], "source": r["source"]} for r in active],
        "definition": "Alert rules that currently fire (capacity, outreach backlog, evidence review share; failed jobs only for system administrators). The thresholds are listed in the Command Center alert rules.",
    }  # fmt: skip


def _kpis_from_core(core: dict[str, Any]) -> list[dict[str, Any]]:
    roll = rollup(core)
    residents_total = sum(r["residents"] for r in roll.values())
    status_counts: dict[str, int] = {}
    priority: dict[str, dict[str, int]] = {p: {"open": 0, "completed": 0, "total": 0} for p in PRIORITY_ORDER}
    for _d, _a, status, prio, n, _pl, _ou in core["cases"]:
        status_counts[status] = status_counts.get(status, 0) + n
        row = priority.setdefault(prio, {"open": 0, "completed": 0, "total": 0})
        row["total"] += n
        row["open"] += n if status not in TERMINAL_STATUSES else 0
        row["completed"] += n if status == "completed" else 0
    cases_total = sum(status_counts.values())
    open_cases = sum(n for s, n in status_counts.items() if s not in TERMINAL_STATUSES)
    completed = status_counts.get("completed", 0)
    in_outreach = sum(status_counts.get(s, 0) for s in OUTREACH_STATUSES)
    residents_open = sum(r["stage"]["open_gaps"] for r in roll.values())

    kpis = [
        metric("residents_total", "Residents", residents_total, "Residents loaded so far (generated; no real person is represented).", "synthetic"),
        metric("residents_with_open_gaps", "Residents with open care gaps", residents_open,
               "Distinct residents with at least one care gap case that is not completed, closed not completed or cancelled.", "synthetic"),
        metric("care_gaps_open", "Open care gaps", open_cases,
               "Care gap cases whose status is not completed, closed_not_completed or cancelled.", "synthetic"),
        metric("care_gaps_total", "Care gaps opened", cases_total, "All care gap cases ever created, any status.", "synthetic"),
        metric("gaps_closed", "Gaps closed", completed,
               "Cases with status completed (only reachable with verified completion evidence; the evidence is simulated).", "simulated"),
        metric("closure_rate", "Closure rate", _ratio(completed, cases_total),
               "Completed cases divided by all cases created (null when no case exists).", "simulated",
               numerator=completed, denominator=cases_total),
        metric("in_outreach", "Currently in outreach", in_outreach,
               "Cases whose status is outreach_pending or outreach_completed (resident contacted, no appointment yet; messages are simulated).", "simulated"),
    ]  # fmt: skip
    return kpis


def _closure_trend(core: dict[str, Any], days: int, now: datetime) -> dict[str, Any]:
    start = (now - timedelta(days=days - 1)).date()
    by_day = {date.fromisoformat(d): tuple(v) for d, v in core["trend"].items() if date.fromisoformat(d) >= start}
    points: list[dict[str, Any]] = []
    if by_day:  # dense series across the window when there is anything to show
        for i in range(days):
            d = start + timedelta(days=i)
            closed, opened = by_day.get(d, (0, 0))
            points.append({"date": d, "closed": closed, "opened": opened})
    return {
        "definition": f"Per UTC day over the last {days} days: closed = status changes to completed; opened = cases created (history rows without a previous status). Empty when no case history exists in the window.",
        "source": "simulated",
        "days": days,
        "points": points,
    }


def _equity(roll: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for d in sorted(roll, key=lambda x: (x == "", x)):
        r = roll[d]
        rows.append({
            "district_code": d or "unknown", "residents": r["residents"], "cases": r["cases"], "planned": r["planned"],
            "completed": r["completed"], "coverage": _ratio(r["planned"], r["cases"]), "closure_rate": _ratio(r["completed"], r["cases"]),
            "due_residents": r["due"], "due_share": _ratio(r["due"], r["residents"]),
        })  # fmt: skip

    def spread(field: str) -> dict[str, Any]:
        vals = [(r[field], r["district_code"]) for r in rows if r[field] is not None]
        if not vals:
            return {"min": None, "max": None, "gap": None}
        lo, hi = min(vals), max(vals)
        return {"min": {"district_code": lo[1], "value": lo[0]}, "max": {"district_code": hi[1], "value": hi[0]}, "gap": round(hi[0] - lo[0], 4)}

    return {
        "definition": "Per district (residents.district_code, an assigned value, not real geography): coverage = cases that reached planning (a slot was proposed) / cases opened; closure_rate = completed / cases opened; due_residents = distinct residents whose latest evaluation is eligible or requires_clinical_review, due_share = due_residents / residents. min/max/gap are over districts that have at least one case; gap is max minus min.",
        "source": "simulated",
        "districts": rows,
        "coverage": spread("coverage"),
        "closure_rate": spread("closure_rate"),
    }


def _recent_activity(session: Session, core: dict[str, Any], internal: bool) -> dict[str, Any]:
    items: list[dict[str, Any]] = [
        {"kind": "case", "at": datetime.fromisoformat(c["at"]), "title": "care_gap.opened" if c["from_status"] is None else f"care_gap.{c['to_status']}",
         "outcome": "success", "resource_type": "care_gap_case", "resource_id": c["case_id"], "actor_role": c["actor_role"], "care_activity": c["care_activity"]}
        for c in core["recent_changes"]
    ]  # fmt: skip  (the role that made the change, never a person)
    if internal:
        audit = session.execute(
            select(AuditEvent)
            .where(AuditEvent.action.notlike("%.read"), AuditEvent.action.notlike("%.view"))
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
            .limit(RECENT_ACTIVITY_LIMIT)
        ).scalars().all()
        jobs = session.execute(select(ProcessingJob).order_by(ProcessingJob.updated_at.desc()).limit(RECENT_ACTIVITY_LIMIT)).scalars().all()
        items += [
            {"kind": "audit", "at": e.occurred_at, "title": e.action, "outcome": e.outcome, "resource_type": e.resource_type,
             "resource_id": e.resource_id, "actor_role": e.actor_role}  # actor ids, request ids and details are never exposed
            for e in audit
        ] + [
            {"kind": "job", "at": j.updated_at, "title": j.job_type, "outcome": j.status, "resource_type": "processing_job",
             "resource_id": str(j.id), "processed_items": j.processed_items, "total_items": j.total_items}
            for j in jobs
        ]  # fmt: skip
        items.sort(key=lambda i: i["at"], reverse=True)
    return {
        "definition": f"The {RECENT_ACTIVITY_LIMIT} most recent care gap status changes, with the role that made each change and never a person's identity."
        + (" For system administrators it also merges state changing audit events and recently updated background jobs." if internal else ""),
        "source": "simulated",
        "items": items[:RECENT_ACTIVITY_LIMIT],
    }


def _alert_rules(session: Session, core: dict[str, Any], now: datetime, internal: bool, districts: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    rules = [_capacity_rule(session, now, districts), _backlog_rule(session, now, districts), _review_rule(core["evidence"]["total"], core["evidence"]["pending"])]
    return [*rules, _failed_jobs_rule(session, now)] if internal else rules  # jobs are internal data


def _rule(key: str, title: str, status: str, severity: str, value: Any, threshold: Any, message: str, definition: str, source: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "title": title, "status": status, "severity": severity if status == "alert" else None, "value": value,
            "threshold": threshold, "message": message, "definition": definition, "source": source, **extra}  # fmt: skip


def _capacity_rule(session: Session, now: datetime, districts: tuple[str, ...] | None = None) -> dict[str, Any]:
    end = now + timedelta(days=CAPACITY_WINDOW_DAYS)
    rows = session.execute(
        select(Clinic.district_code, func.coalesce(func.sum(CapacitySlot.available_capacity), 0), func.coalesce(func.sum(CapacitySlot.reserved_capacity), 0))
        .select_from(CapacitySlot)
        .join(Clinic, Clinic.id == CapacitySlot.clinic_id)
        .where(CapacitySlot.slot_status == "open", CapacitySlot.start_time >= now, CapacitySlot.start_time < end, *([Clinic.district_code.in_(districts)] if districts is not None else []))
        .group_by(Clinic.district_code)
    ).all()
    seats = sum(int(a) for _, a, _ in rows)
    reserved = sum(int(r) for _, _, r in rows)
    util = _ratio(reserved, seats)
    hot = sorted(d for d, a, r in rows if a and r / a >= CAPACITY_UTILISATION_ALERT)
    status = "no_data" if not seats else "alert" if util >= CAPACITY_UTILISATION_ALERT else "ok"
    return _rule(
        "capacity_utilisation", "Clinic capacity nearly full", status, "critical" if (util or 0) >= 1 else "warning", util, CAPACITY_UTILISATION_ALERT,
        f"{reserved} of {seats} simulated seats in the next {CAPACITY_WINDOW_DAYS} days are reserved ({util:.0%})." if seats else f"No simulated capacity is registered for the next {CAPACITY_WINDOW_DAYS} days.",
        f"Reserved (confirmed) seats / available seats over open capacity slots starting in the next {CAPACITY_WINDOW_DAYS} days; alert at >= {CAPACITY_UTILISATION_ALERT:.0%}, critical at 100%.",
        "simulated", seats=seats, reserved_seats=reserved, districts_at_or_over_threshold=hot,
    )  # fmt: skip


def _backlog_rule(session: Session, now: datetime, districts: tuple[str, ...] | None = None) -> dict[str, Any]:
    cutoff = now - timedelta(days=OUTREACH_BACKLOG_DAYS)
    stmt = select(func.count(), func.min(CareGapCase.updated_at)).where(CareGapCase.status == "planned", CareGapCase.updated_at < cutoff)
    if districts is not None:
        stmt = stmt.join(Resident, Resident.id == CareGapCase.resident_id).where(Resident.district_code.in_(districts))
    n, oldest = session.execute(stmt).one()
    return _rule(
        "outreach_backlog", "Outreach backlog", "alert" if n else "ok", "warning", n, 0,
        f"{n} planned case(s) have waited more than {OUTREACH_BACKLOG_DAYS} days without outreach." if n else "No planned case has waited past the backlog limit.",
        f"Cases still in status planned (no outreach started) whose last status change is older than {OUTREACH_BACKLOG_DAYS} days; alert when any exist.",
        "simulated", oldest_status_change_at=oldest, max_age_days=OUTREACH_BACKLOG_DAYS,
    )  # fmt: skip


def _review_rule(total: int, pending: int) -> dict[str, Any]:
    share = _ratio(pending, total)
    status = "no_data" if total < EVIDENCE_REVIEW_MIN_FACTS else "alert" if share >= EVIDENCE_REVIEW_SHARE_ALERT else "ok"
    return _rule(
        "evidence_review_share", "Evidence awaiting review", status, "warning", share, EVIDENCE_REVIEW_SHARE_ALERT,
        f"{pending} of {total} extracted facts ({share:.0%}) await human review." if total else "No evidence facts have been extracted yet.",
        f"Facts with validation_status requires_review and review_status pending / all evidence facts; alert at >= {EVIDENCE_REVIEW_SHARE_ALERT:.0%} once at least {EVIDENCE_REVIEW_MIN_FACTS} facts exist.",
        "synthetic", pending_facts=pending, total_facts=total, min_facts=EVIDENCE_REVIEW_MIN_FACTS,
    )  # fmt: skip


def _failed_jobs_rule(session: Session, now: datetime) -> dict[str, Any]:
    since = now - timedelta(hours=FAILED_JOBS_WINDOW_HOURS)
    n = session.scalar(select(func.count()).select_from(ProcessingJob).where(ProcessingJob.status == "failed", ProcessingJob.updated_at >= since)) or 0
    return _rule(
        "jobs_failed", "Failed background jobs", "alert" if n else "ok", "warning", n, 0,
        f"{n} background job(s) failed in the last {FAILED_JOBS_WINDOW_HOURS} hours." if n else f"No background job failed in the last {FAILED_JOBS_WINDOW_HOURS} hours.",
        f"Jobs with status failed whose last update is within {FAILED_JOBS_WINDOW_HOURS} hours; alert when any exist.",
        "live", window_hours=FAILED_JOBS_WINDOW_HOURS,
    )  # fmt: skip


# ------------------------------------------------------------------------------------------------ funnel
def funnel(
    session: Session, *, district: str | None = None, state: str | None = None, compare_days: int | None = None,
    sessions: Callable[[], Session] | None = None, now: datetime | None = None,
) -> dict[str, Any]:  # fmt: skip
    """`compare_days` (7, 14, 30, 60 or 90) adds the stage counts as they stood that many days ago and the change since."""
    if compare_days is not None and compare_days not in COMPARE_DAYS:
        raise ValidationFailed("Unsupported comparison period.", [{"field": "days", "reason": f"with compare=previous, one of {list(COMPARE_DAYS)}"}])
    scope = scope_info(district, state)
    return cached(("funnel", scope["district"], scope["state"], compare_days), FUNNEL_TTL_S, lambda: _funnel(session, sessions, scope, compare_days, now or _now()))


def _funnel(session: Session, sessions: Callable[[], Session] | None, scope: dict[str, Any], compare_days: int | None, now: datetime) -> dict[str, Any]:
    core, meta = load_core(session, sessions)
    core = scoped_core(core, None if scope["districts"] is None else tuple(scope["districts"]))
    roll = rollup(core)
    residents = sum(r["residents"] for r in roll.values())
    evaluated = sum(r["evaluated"] for r in roll.values())
    due = sum(r["due"] for r in roll.values())
    stage_sum = {k: sum(r["stage"][k] for r in roll.values()) for k in ("opened", "planned", "outreach", "closed")}
    cases = {"opened": 0, "planned": 0, "outreach": 0, "closed": 0}
    leakage = {"closed_not_completed": 0, "cancelled": 0, "escalated": 0}
    for _d, _a, status, _p, n, planned, outreach in core["cases"]:
        cases["opened"] += n
        cases["planned"] += planned
        cases["outreach"] += outreach
        cases["closed"] += n if status == "completed" else 0
        if status in leakage:
            leakage[status] += n

    def stage(key: str, label: str, count: int, definition: str, source: str, case_count: int | None = None) -> dict[str, Any]:
        return {"key": key, "label": label, "count": count, "cases": case_count, "unit": "residents", "share_of_residents": _ratio(count, residents),
                "definition": definition, "source": source}  # fmt: skip

    stages = [
        stage("residents", "Residents", residents, "Rows in residents.", "synthetic"),
        stage("evaluated", "Evaluated", evaluated, "Distinct residents with at least one stored eligibility evaluation (any guideline version).", "synthetic"),
        stage("due", "Due", due, "Distinct residents whose latest evaluation of at least one guideline version is eligible or requires_clinical_review (deterministic rules; superseded evaluations ignored).", "synthetic"),
        stage("case_opened", "Care gap case opened", stage_sum["opened"], "Distinct residents with at least one care gap case ever created (any status). `cases` counts the cases.", "synthetic", cases["opened"]),
        stage("planned", "Planned", stage_sum["planned"], "Distinct residents with a case that is, or ever was, planned (a simulated slot was proposed) or beyond.", "simulated", cases["planned"]),
        stage("in_outreach", "In outreach", stage_sum["outreach"], "Distinct residents with a case that is, or ever was, in outreach_pending or beyond (messages are simulated).", "simulated", cases["outreach"]),
        stage("closed", "Closed", stage_sum["closed"], "Distinct residents with a case whose status is completed (verified completion evidence; simulated).", "simulated", cases["closed"]),
    ]
    for prev, cur in zip(stages, stages[1:], strict=False):
        cur["conversion_from_previous"] = _ratio(cur["count"], prev["count"])
    stages[0]["conversion_from_previous"] = None
    comparison = _funnel_comparison(core, stages, compare_days) if compare_days is not None else None
    return {
        "generated_at": now,
        "cache_ttl_seconds": FUNNEL_TTL_S,
        "comparison": comparison,
        "snapshot": snapshot_meta(meta),
        "scope": scope,
        "synthetic_banner": SYNTHETIC_BANNER,
        "sources": SOURCES,
        "note": "Each stage is an independent count of distinct residents; case stages use the append only status history ('ever reached'), so they are cumulative. After a new evaluation changes an outcome a later stage can exceed an earlier one.",
        "stages": stages,
        "leakage": {
            "definition": "Cases currently in a status that leaves the funnel without completion (counted as cases, not residents).",
            "source": "simulated",
            "items": [{"status": s, "cases": leakage[s]} for s in ("closed_not_completed", "cancelled", "escalated")],
        },
    }


def _funnel_comparison(core: dict[str, Any], stages: list[dict[str, Any]], days: int) -> dict[str, Any]:
    """Adds `previous` {count, cases, delta, delta_pct} to the stages that can be rebuilt for the start of the period."""
    snap = (core.get("funnel_previous") or {}).get(str(days))
    stock = (core.get("stocks") or {}).get(str(days))
    base: dict[str, Any] = {
        "window_days": days, "source": "simulated",
        "definition": f"Each stage as it stood {days} days ago, rebuilt from the append only case status history (first time a case reached the stage) and the evaluation times, with the change since. Closed is the cases completed at that moment.",
        "not_available": {"residents": "Residents are loaded in bulk; no history of the population over time is kept.",
                          "due": "Residents due depends on the latest evaluation outcome as of that date, which is not stored per date."},
    }  # fmt: skip
    for st in stages:
        st["previous"] = None
    if snap is None or stock is None:
        return {**base, "available": False, "reason": "The stored snapshot predates period comparison; a refresh is pending."}
    previous = {
        "evaluated": (snap["evaluated"], None), "case_opened": tuple(snap["opened"]), "planned": tuple(snap["planned"]), "in_outreach": tuple(snap["outreach"]),
        "closed": (stock.get("closed_residents", 0), stock["statuses"].get("completed", 0)),
    }  # fmt: skip
    for st in stages:
        if st["key"] in previous:
            count, cases = previous[st["key"]]
            st["previous"] = {"count": count, "cases": cases, "delta": st["count"] - count, "delta_pct": _ratio((st["count"] - count) * 100, count, 1)}
    return {**base, "available": True, "previous_as_of": snap["as_of"]}


# ------------------------------------------------------------------------------------------------ processing monitor
def processing(session: Session, *, dataset_version: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    return cached(("processing", dataset_version), PROCESSING_TTL_S, lambda: _processing(session, dataset_version, now or _now()))


def _processing(session: Session, dataset_version: str | None, now: datetime) -> dict[str, Any]:
    return {
        "generated_at": now,
        "cache_ttl_seconds": PROCESSING_TTL_S,
        "synthetic_banner": SYNTHETIC_BANNER,
        "sources": SOURCES,
        "pipeline": _pipeline(session, dataset_version, now),
        "jobs": _jobs(session, now),
        "ai_spend": _ai_spend(session, now),
        "documents": _documents(session),
    }


def _latest_manifest_version() -> str | None:
    files = list(synthea.SYNTHEA_DIR.glob("*/manifest.json")) if synthea.SYNTHEA_DIR.exists() else []
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime).parent.name


def _throughput_per_hour(entries: list[dict[str, Any]]) -> float | None:
    """Residents/hour from the inter-arrival times of the most recent completed (non-recovered) generation batches."""
    done = sorted(
        (b for b in entries if b.get("status") == "completed" and not b.get("recovered") and b.get("finished_at")),
        key=lambda b: b["finished_at"],
    )[-(THROUGHPUT_BATCHES + 1) :]
    if len(done) < 2:
        return None
    span = (datetime.fromisoformat(done[-1]["finished_at"]) - datetime.fromisoformat(done[0]["finished_at"])).total_seconds()
    return sum(b["residents"] for b in done[1:]) / span * 3600 if span > 0 else None


def _eta_hours(remaining: int, rate: float | None) -> float | None:
    if remaining <= 0:
        return 0.0
    return round(remaining / rate, 1) if rate else None


def _pipeline(session: Session, requested: str | None, now: datetime) -> dict[str, Any]:
    version = requested or _latest_manifest_version()
    base: dict[str, Any] = {
        "source": "synthetic",
        "definition": "Synthea generation progress from data/synthea/<dataset_version>/manifest.json; loading progress from datasets.manifest.ingest and live row counts.",
        "dataset_version": version,
    }
    manifest = synthea.load_manifest(synthea.SYNTHEA_DIR / version) if version else None
    if manifest is None:
        return {**base, "available": False, "message": "No generation manifest found for this dataset version."}
    target, batch_size = int(manifest["resident_target"]), int(manifest["batch_size"])
    entries = list(manifest["batches"].values())
    completed = [b for b in entries if b.get("status") == "completed"]
    generated = sum(b["residents"] for b in completed)
    planned_batches = math.ceil(target / batch_size)
    gen_rate = _throughput_per_hour(entries)
    updated = datetime.fromisoformat(manifest["updated_at"]) if manifest.get("updated_at") else None
    idle_s = round((now - updated).total_seconds()) if updated else None

    ingest = _ingest(session, version, target, now)
    loaded = ingest["residents_loaded"]
    gen_eta, load_eta = _eta_hours(target - generated, gen_rate), ingest["eta_hours"]
    complete = loaded >= target
    active = idle_s is not None and idle_s <= ACTIVE_WINDOW_S
    return {
        **base,
        "available": True,
        "state": "complete" if complete else "running" if active else "idle",
        "manifest_status": manifest.get("status"),
        "resident_target": target,
        "batch_size": batch_size,
        "manifest_updated_at": updated,
        "idle_seconds": idle_s,
        "generation": {
            "batches_planned": planned_batches,
            "batches_completed": len(completed),
            "batches_running": sum(b.get("status") == "running" for b in entries),
            "batches_failed": sum(b.get("status") == "failed" for b in entries),
            "residents_generated": generated,
            "percent": _ratio(generated, target),
            "throughput_residents_per_hour": round(gen_rate) if gen_rate else None,
            "eta_hours": gen_eta,
            "throughput_basis": f"spacing of the last {THROUGHPUT_BATCHES} completed batches (recovered batches excluded)",
        },
        "ingestion": {**ingest, "percent": _ratio(loaded, target)},
        "eta_hours": None if gen_eta is None or load_eta is None else max(gen_eta, load_eta),
        "eta_basis": "the slower of generation and loading at their recent rates; null when a rate is unknown",
    }


def _ingest(session: Session, version: str, target: int, now: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {
        "dataset_loaded": False, "batches_loaded": 0, "residents_loaded": 0, "profiles": {"full": 0, "rules": 0},
        "throughput_residents_per_hour": None, "eta_hours": None,
    }  # fmt: skip
    ds = session.execute(select(Dataset.id, Dataset.status).where(Dataset.dataset_version == version)).first()
    if ds is None:
        return {**out, "eta_hours": None}
    since = now - timedelta(hours=1)
    rows = session.execute(
        text(
            "SELECT coalesce(b.value->>'profile', 'full') AS profile, count(*) AS batches, "
            "coalesce(sum((b.value->>'residents_staged')::int), 0) AS residents, "
            "coalesce(sum((b.value->>'residents_staged')::int) FILTER (WHERE (b.value->>'loaded_at')::timestamptz >= :since), 0) AS recent "
            "FROM anticipa.datasets d, jsonb_each(d.manifest->'ingest'->'batches') b WHERE d.id = :id GROUP BY 1"
        ),
        {"id": ds.id, "since": since},
    ).all()
    loaded = session.scalar(select(func.count()).select_from(Resident).where(Resident.dataset_id == ds.id)) or 0
    recent = sum(int(r.recent) for r in rows)
    rate = float(recent) if recent else None  # residents loaded in the last hour = residents per hour
    return {
        **out,
        "dataset_loaded": True,
        "dataset_status": ds.status,
        "batches_loaded": sum(int(r.batches) for r in rows),
        "residents_loaded": loaded,
        "profiles": {"full": 0, "rules": 0, **{r.profile: int(r.residents) for r in rows}},
        "profiles_basis": "residents staged per storage profile, from the loader's per batch manifest (full = all clinical detail; rules = rule relevant detail only)",
        "throughput_residents_per_hour": round(rate) if rate else None,
        "throughput_basis": "residents loaded in the last hour",
        "eta_hours": _eta_hours(target - loaded, rate),
    }


def _jobs(session: Session, now: datetime) -> dict[str, Any]:
    by_status = dict(session.execute(select(ProcessingJob.status, func.count()).group_by(ProcessingJob.status)).all())
    since = now - timedelta(hours=FAILED_JOBS_WINDOW_HOURS)
    failed_recent = session.scalar(select(func.count()).select_from(ProcessingJob).where(ProcessingJob.status == "failed", ProcessingJob.updated_at >= since)) or 0

    def out(j: ProcessingJob) -> dict[str, Any]:
        return {
            "id": j.id, "job_type": j.job_type, "status": j.status, "total_items": j.total_items, "processed_items": j.processed_items,
            "succeeded_items": j.succeeded_items, "failed_items": j.failed_items,
            "percent_complete": _ratio(j.processed_items, j.total_items) if j.total_items else None,
            "created_at": j.created_at, "started_at": j.started_at, "updated_at": j.updated_at, "completed_at": j.completed_at,
            "heartbeat_age_seconds": round((now - j.heartbeat_at).total_seconds()) if j.heartbeat_at else None,
            "error_summary": (j.error_summary or "")[:200] or None,
        }  # fmt: skip

    active = session.execute(
        select(ProcessingJob).where(ProcessingJob.status.in_(("pending", "queued", "running"))).order_by(ProcessingJob.created_at.desc()).limit(10)
    ).scalars().all()
    recent = session.execute(select(ProcessingJob).order_by(ProcessingJob.created_at.desc()).limit(8)).scalars().all()
    return {
        "source": "live",
        "definition": "Rows of processing_jobs: counts by status, active jobs (pending, queued, running) with item progress, the most recent jobs, and failures within the last "
        f"{FAILED_JOBS_WINDOW_HOURS} hours.",
        "by_status": by_status,
        "failed_last_24h": failed_recent,
        "active": [out(j) for j in active],
        "recent": [out(j) for j in recent],
    }


def _ai_spend(session: Session, now: datetime) -> dict[str, Any]:
    cfg = get_ai_settings()
    rows = session.execute(
        select(
            ModelInvocation.provider, ModelInvocation.model_id, func.count(), func.coalesce(func.sum(ModelInvocation.cost_usd), 0),
            func.coalesce(func.sum(ModelInvocation.input_tokens), 0), func.coalesce(func.sum(ModelInvocation.output_tokens), 0),
            func.count().filter(ModelInvocation.cache_hit.is_(True)), func.count().filter(ModelInvocation.status == "failed"),
        ).group_by(ModelInvocation.provider, ModelInvocation.model_id)
    ).all()  # fmt: skip
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    spent_today = _f(session.scalar(select(func.sum(ModelInvocation.cost_usd)).where(ModelInvocation.created_at >= today)))
    run_cost = func.sum(ModelInvocation.cost_usd)
    top = session.execute(
        select(ModelInvocation.job_id, run_cost).where(ModelInvocation.job_id.is_not(None)).group_by(ModelInvocation.job_id).order_by(desc(run_cost)).limit(1)
    ).first()
    by_provider: dict[str, float] = {}
    models = []
    for provider, model_id, calls, cost, tin, tout, hits, failed in rows:
        by_provider[provider] = by_provider.get(provider, 0.0) + _f(cost)
        models.append({"provider": provider, "model_id": model_id, "calls": calls, "cost_usd": round(_f(cost), 4), "input_tokens": int(tin),
                       "output_tokens": int(tout), "cache_hits": hits, "failed_calls": failed})  # fmt: skip
    total = sum(by_provider.values())
    run_budget, daily_budget = _f(cfg.anticipa_ai_run_budget_usd), _f(cfg.anticipa_ai_daily_budget_usd)
    nebius_spend, nebius_credit = by_provider.get("nebius", 0.0), _f(cfg.anticipa_nebius_credit_usd)
    largest = _f(top[1]) if top else 0.0
    return {
        "source": "live",
        "definition": "Sums of model_invocations.cost_usd (cost recorded by the Model Router per call; cached replays cost nothing) compared with the configured caps (ANTICIPA_AI_RUN_BUDGET_USD, ANTICIPA_AI_DAILY_BUDGET_USD, ANTICIPA_NEBIUS_CREDIT_USD).",
        "total_usd": round(total, 4),
        "by_provider": {p: round(v, 4) for p, v in sorted(by_provider.items())},
        "models": sorted(models, key=lambda m: -m["cost_usd"]),
        "invocations": sum(m["calls"] for m in models),
        "anthropic": {
            "spend_usd": round(by_provider.get("anthropic", 0.0), 4),
            "run_budget_usd": run_budget,
            "largest_run_spend_usd": round(largest, 4),
            "largest_run_share_of_budget": _ratio(largest, run_budget),
            "run_budget_basis": "the per run cap applies to one job run; compared here with the most expensive run recorded so far (grouped by job_id)",
            "spent_today_usd": round(spent_today, 4),
            "daily_budget_usd": daily_budget,
            "daily_share_of_budget": _ratio(spent_today, daily_budget),
        },
        "nebius": {
            "enabled": bool(cfg.anticipa_enable_nebius),
            "spend_usd": round(nebius_spend, 4),
            "credit_usd": nebius_credit,
            "remaining_usd": round(max(nebius_credit - nebius_spend, 0.0), 4),
            "share_of_credit_used": _ratio(nebius_spend, nebius_credit),
        },
    }


def _documents(session: Session) -> dict[str, Any]:
    docs = dict(session.execute(select(Document.processing_status, func.count()).group_by(Document.processing_status)).all())
    facts = dict(session.execute(select(EvidenceFact.validation_status, func.count()).group_by(EvidenceFact.validation_status)).all())
    return {
        "source": "synthetic",
        "definition": "Counts of generated documents by processing_status and of extracted evidence facts by validation_status.",
        "documents_total": sum(docs.values()),
        "documents_by_status": docs,
        "evidence_facts_total": sum(facts.values()),
        "evidence_facts_by_validation_status": facts,
    }



# ------------------------------------------------------------------------------------------------ demand summary
def demand_summary(
    session: Session, *, district: str | None = None, state: str | None = None, sessions: Callable[[], Session] | None = None, now: datetime | None = None
) -> dict[str, Any]:
    scope = scope_info(district, state)
    return cached(("demand", scope["district"], scope["state"]), DEMAND_TTL_S, lambda: _demand_summary(session, sessions, scope, now or _now()))


def _demand_summary(session: Session, sessions: Callable[[], Session] | None, scope: dict[str, Any], now: datetime) -> dict[str, Any]:
    core, meta = load_core(session, sessions)
    core = scoped_core(core, None if scope["districts"] is None else tuple(scope["districts"]))
    by_programme: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    for _d, activity, status, prio, n, _pl, _ou in core["cases"]:
        if status in PLANNABLE:
            by_programme[activity] = by_programme.get(activity, 0) + n
            by_priority[prio] = by_priority.get(prio, 0) + n
    appointments = sum(by_programme.values())
    residents = sum(r["stage"].get("plannable", 0) for r in rollup(core).values())
    return {
        "generated_at": now, "cache_ttl_seconds": DEMAND_TTL_S, "snapshot": snapshot_meta(meta), "scope": scope, "synthetic_banner": SYNTHETIC_BANNER, "sources": SOURCES,
        "target_population": {"label": "Target population", "value": residents, "source": "synthetic",
                              "definition": "Distinct residents with at least one approved care gap case that is waiting for a slot (approved_for_planning or awaiting_capacity)."},
        "appointments_required": {"label": "Appointments required", "value": appointments, "source": "synthetic",
                                  "definition": "Approved care gap cases waiting for a slot; one appointment per case, so a resident with two programmes needs two."},
        "by_programme": [{"care_activity": a, "label": programme_label(a), "appointments_required": n} for a, n in sorted(by_programme.items(), key=lambda x: (-x[1], x[0]))],
        "by_priority": [{"priority": p, "appointments_required": by_priority.get(p, 0)} for p in PRIORITY_ORDER],
    }  # fmt: skip
