"""Precomputed aggregates behind the Command Center, funnel, geography and programme endpoints.

`compute_core` runs the heavy aggregate queries once (a few seconds on 200k residents, 1.2M evaluations and 116k cases)
and returns plain JSON: counts per district x programme (care activity) x status x priority, distinct resident stage
counts per district, evidence review counts, a sparse daily closure trend and the latest status changes. `refresh` stores
it in `analytics_snapshots` (migration 0008); the API reads that one row (milliseconds) and shapes every endpoint from it.

Freshness: the CLI (`python -m anticipa.analytics refresh`), the worker after a job of an analytics relevant module, and
a stale check in the API (the table change counters of the watched tables differ from the snapshot's fingerprint) all
refresh it. A GET never waits for a refresh: it serves the current snapshot and, if the data changed, starts one
background refresh per process. Without any snapshot (or before migration 0008) it computes live, as before.
ponytail: one snapshot row and one background thread per API process; use a scheduler if the API scales out.
"""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, cast, distinct, exists, extract, func, literal_column, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, aliased

from anticipa.analytics.models import AnalyticsSnapshot
from anticipa.analytics.places import district_map
from anticipa.caregaps.models import TERMINAL_STATUSES, CareGapCase, CaseStatusHistory
from anticipa.caregaps.service import CASE_OUTCOMES
from anticipa.datafabric.models import Dataset, Resident
from anticipa.datafabric.routes import AGE_BANDS
from anticipa.evidence.models import EvidenceFact
from anticipa.guidelines.models import EligibilityEvaluation, GuidelineVersion

log = logging.getLogger(__name__)

CORE_KEY = "core"
CORE_VERSION = 1
AUTO_REFRESH = True  # tests switch it off: a GET must not start a background refresh there
AUTO_REFRESH_GAP_S = 120.0  # at most one automatic refresh per this many seconds (per process)
JOB_REFRESH_GAP_S = 30.0  # the worker skips a refresh when the snapshot is younger than this
TREND_DAYS = 180  # daily closure points kept in the snapshot (a 90 day window plus the 90 days before it)
COMPARE_DAYS = (7, 14, 30, 60, 90)  # period lengths for which the status counts at the start are stored
RECENT_CHANGES = 10
REFRESH_JOB_PREFIXES = ("guidelines.", "caregaps.", "planning.", "engagement.", "closure.", "evidence.", "datafabric.")
WATCHED_TABLES = (
    "residents", "eligibility_evaluations", "care_gap_cases", "case_status_history", "evidence_facts", "planned_allocations",
    "communication_events", "appointments", "completion_evidence",
)  # fmt: skip
_ADVISORY_LOCK = 7_283_402
DONE_OUTCOME = "already_completed"

OUTREACH_STATUSES = ("outreach_pending", "outreach_completed")  # currently contacting the resident
OUTREACH_OR_LATER = (*OUTREACH_STATUSES, "appointment_scheduled", "awaiting_completion_evidence", "completed")
ATTENDED = ("awaiting_completion_evidence", "completed")  # the appointment was attended
PLANNABLE_STATUSES = ("approved_for_planning", "awaiting_capacity")  # approved and waiting for a slot
PLANNED_OR_LATER = ("planned", *OUTREACH_OR_LATER)


def reached(statuses: tuple[str, ...]) -> Any:
    """SQL condition on `care_gap_cases`: the case is in one of `statuses` now or ever was (append only history).
    An uncorrelated IN (one hashed lookup) is several times faster than a correlated EXISTS per case."""
    h = aliased(CaseStatusHistory)
    return or_(CareGapCase.status.in_(statuses), CareGapCase.id.in_(select(h.case_id).where(h.to_status.in_(statuses))))


def _key(value: str | None) -> str:
    return value or ""


def reference_date(session: Session) -> date:
    """Ages are computed as of the dataset's generation reference date, like the population overview (not the wall clock)."""
    manifest = session.scalar(select(Dataset.manifest).order_by(Dataset.created_at.desc()).limit(1)) or {}
    ref = (manifest.get("generation") or {}).get("reference_date")
    try:
        return date(int(ref[:4]), int(ref[4:6]), int(ref[6:8])) if ref else datetime.now(UTC).date()
    except (TypeError, ValueError):
        return datetime.now(UTC).date()


def band_and_sex(as_of: date) -> tuple[Any, Any]:
    """SQL expressions (no bind parameters, so they can be repeated in SELECT and GROUP BY) for the age band and sex."""
    age = f"date_part('year', age(DATE '{as_of.isoformat()}', anticipa.residents.birth_date))"  # as_of is a date: no injection
    whens = " ".join(
        f"WHEN {age} >= {lo} AND {age} <= {hi} THEN '{label}'" if hi is not None else f"WHEN {age} >= {lo} THEN '{label}'"
        for label, lo, hi in AGE_BANDS
    )
    return literal_column(f"CASE {whens} ELSE 'unknown' END"), literal_column("coalesce(anticipa.residents.administrative_sex, 'unknown')")


# ------------------------------------------------------------------------------------------------ compute
def fingerprint(session: Session) -> int:
    """A cheap change counter: inserts + updates + deletes recorded for the watched tables (pg_stat_user_tables)."""
    value = session.scalar(
        text(
            "SELECT coalesce(sum(n_tup_ins + n_tup_upd + n_tup_del), 0) FROM pg_stat_user_tables "
            "WHERE schemaname = 'anticipa' AND relname = ANY(:tables)"
        ),
        {"tables": list(WATCHED_TABLES)},
    )
    return int(value or 0)


def compute_core(session: Session, now: datetime | None = None) -> dict[str, Any]:
    started, now = time.perf_counter(), now or datetime.now(UTC)
    fp = fingerprint(session)  # taken first: a change during the compute makes the next check refresh again
    district = Resident.district_code
    planned_cond, outreach_cond = reached(PLANNED_OR_LATER), reached(OUTREACH_OR_LATER)

    residents = {_key(d): n for d, n in session.execute(select(district, func.count()).group_by(district))}
    grid = session.execute(
        select(
            district, CareGapCase.care_activity, CareGapCase.status, CareGapCase.priority, func.count(),
            func.count().filter(planned_cond), func.count().filter(outreach_cond),
        )
        .select_from(CareGapCase)
        .join(Resident, Resident.id == CareGapCase.resident_id)
        .group_by(district, CareGapCase.care_activity, CareGapCase.status, CareGapCase.priority)
    ).all()  # fmt: skip
    rid = CareGapCase.resident_id
    stages = {
        _key(d): {"opened": o, "planned": p, "outreach": ou, "closed": c, "open_gaps": og, "plannable": pl}
        for d, o, p, ou, c, og, pl in session.execute(
            select(
                district, func.count(distinct(rid)), func.count(distinct(rid)).filter(planned_cond),
                func.count(distinct(rid)).filter(outreach_cond), func.count(distinct(rid)).filter(CareGapCase.status == "completed"),
                func.count(distinct(rid)).filter(CareGapCase.status.notin_(TERMINAL_STATUSES)),
                func.count(distinct(rid)).filter(CareGapCase.status.in_(PLANNABLE_STATUSES)),
            )
            .select_from(CareGapCase)
            .join(Resident, Resident.id == CareGapCase.resident_id)
            .group_by(district)
        )
    }  # fmt: skip
    # OFFSET 0 is an optimisation fence: the evaluation scans run first, the (small) results are then joined to residents.
    evaluated_ids = select(EligibilityEvaluation.resident_id.label("resident_id")).group_by(EligibilityEvaluation.resident_id).offset(0).subquery("evaluated_ids")
    evaluated = {
        _key(d): n
        for d, n in session.execute(
            select(district, func.count()).select_from(evaluated_ids).join(Resident, Resident.id == evaluated_ids.c.resident_id).group_by(district)
        )
    }
    newer = aliased(EligibilityEvaluation)
    superseded = exists().where(
        newer.resident_id == EligibilityEvaluation.resident_id,
        newer.guideline_version_id == EligibilityEvaluation.guideline_version_id,
        tuple_(newer.evaluated_at, newer.id) > tuple_(EligibilityEvaluation.evaluated_at, EligibilityEvaluation.id),
    ).correlate(EligibilityEvaluation)
    as_of = reference_date(session)
    band, sex = band_and_sex(as_of)
    due_pairs = (
        select(
            EligibilityEvaluation.resident_id.label("resident_id"), EligibilityEvaluation.guideline_version_id.label("guideline_version_id"),
            EligibilityEvaluation.outcome.label("outcome"),
        )
        .where(EligibilityEvaluation.outcome.in_((*CASE_OUTCOMES, DONE_OUTCOME)), ~superseded)
        .offset(0)
        .subquery("due_pairs")
    )
    due_n = func.count(distinct(due_pairs.c.resident_id)).filter(due_pairs.c.outcome.in_(CASE_OUTCOMES))
    done_n = func.count(distinct(due_pairs.c.resident_id)).filter(due_pairs.c.outcome == DONE_OUTCOME)
    due: dict[str, int] = {}
    due_by_activity: list[list[Any]] = []  # [district, care_activity, due residents, residents already up to date]
    cohort_due: dict[tuple[str, str, str], int] = {}
    for d, activity, b, sx, n_due, n_done in session.execute(  # one scan, three groupings: district; district x programme; district x band x sex
        select(district, GuidelineVersion.care_activity, band, sex, due_n, done_n)
        .select_from(due_pairs)
        .join(Resident, Resident.id == due_pairs.c.resident_id)
        .join(GuidelineVersion, GuidelineVersion.id == due_pairs.c.guideline_version_id)
        .group_by(func.grouping_sets(tuple_(district), tuple_(district, GuidelineVersion.care_activity), tuple_(district, band, sex)))
    ):
        if activity is not None:
            due_by_activity.append([_key(d), activity, n_due, n_done])
        elif b is not None:
            cohort_due[(_key(d), b, sx)] = n_due
        else:
            due[_key(d)] = n_due
    attended_cond = reached(ATTENDED)
    cohorts: dict[tuple[str, str, str], list[int]] = {}  # [residents, cases, open, completed, in_outreach, planned, due, attended]

    def cohort(d: str | None, b: str, sx: str) -> list[int]:
        return cohorts.setdefault((_key(d), b, sx), [0, 0, 0, 0, 0, 0, 0, 0])

    for d, b, sx, n in session.execute(select(district, band, sex, func.count()).group_by(district, band, sex)):
        cohort(d, b, sx)[0] = n
    for d, b, sx, n, n_open, n_done, n_out, n_plan, n_att in session.execute(
        select(
            district, band, sex, func.count(), func.count().filter(CareGapCase.status.notin_(TERMINAL_STATUSES)),
            func.count().filter(CareGapCase.status == "completed"), func.count().filter(CareGapCase.status.in_(OUTREACH_STATUSES)),
            func.count().filter(planned_cond), func.count().filter(attended_cond),
        )
        .select_from(CareGapCase)
        .join(Resident, Resident.id == CareGapCase.resident_id)
        .group_by(district, band, sex)
    ):
        cohort(d, b, sx)[1:6] = [n, n_open, n_done, n_out, n_plan]
        cohort(d, b, sx)[7] = n_att
    for (d, b, sx), n in cohort_due.items():
        cohort(d, b, sx)[6] = n
    def history_by_district(*cols: Any) -> Any:
        """History rows joined to their case and resident, so every figure can be split by district."""
        return (
            select(*cols).select_from(CaseStatusHistory).join(CareGapCase, CareGapCase.id == CaseStatusHistory.case_id).join(Resident, Resident.id == CareGapCase.resident_id)
        )

    stocks: dict[str, Any] = {}
    for days in COMPARE_DAYS:  # the status every case had at the start of each comparison period (append only history)
        at = now - timedelta(days=days)
        latest = (
            select(CaseStatusHistory.case_id, CaseStatusHistory.to_status)
            .where(CaseStatusHistory.occurred_at <= at)
            .ext(distinct_on(CaseStatusHistory.case_id))
            .order_by(CaseStatusHistory.case_id, CaseStatusHistory.occurred_at.desc(), CaseStatusHistory.id.desc())
            .subquery()
        )
        by_district: dict[str, dict[str, int]] = {}
        closed_residents: dict[str, int] = {}
        for d, status, n, n_residents in session.execute(
            select(district, latest.c.to_status, func.count(), func.count(distinct(CareGapCase.resident_id)))
            .select_from(latest)
            .join(CareGapCase, CareGapCase.id == latest.c.case_id)
            .join(Resident, Resident.id == CareGapCase.resident_id)
            .group_by(district, latest.c.to_status)
        ):
            by_district.setdefault(_key(d), {})[status] = n
            if status == "completed":
                closed_residents[_key(d)] = n_residents
        stocks[str(days)] = {"as_of": at.isoformat(), "by_district": by_district, "closed_residents": closed_residents}
    funnel_previous = _funnel_previous(session, district, now)
    facts_total = session.scalar(select(func.count()).select_from(EvidenceFact)) or 0
    facts_pending_cond = (EvidenceFact.validation_status == "requires_review", EvidenceFact.review_status == "pending")
    facts_pending = session.scalar(select(func.count()).select_from(EvidenceFact).where(*facts_pending_cond)) or 0
    evidence_by_district = {
        _key(d): [n, pending]
        for d, n, pending in session.execute(
            select(district, func.count(), func.count().filter(*facts_pending_cond))
            .select_from(EvidenceFact)
            .join(Resident, Resident.id == EvidenceFact.resident_id)
            .group_by(district)
        )
    }  # fmt: skip
    day = cast(func.timezone("UTC", CaseStatusHistory.occurred_at), Date)
    since = (now - timedelta(days=TREND_DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    trend_by_district: dict[str, dict[str, list[int]]] = {}
    for d, dy, closed, opened in session.execute(
        history_by_district(district, day, func.count().filter(CaseStatusHistory.to_status == "completed"), func.count().filter(CaseStatusHistory.from_status.is_(None)))
        .where(CaseStatusHistory.occurred_at >= since, or_(CaseStatusHistory.to_status == "completed", CaseStatusHistory.from_status.is_(None)))
        .group_by(district, day)
    ):
        trend_by_district.setdefault(_key(d), {})[dy.isoformat()] = [closed, opened]
    rn = func.row_number().over(partition_by=district, order_by=(CaseStatusHistory.occurred_at.desc(), CaseStatusHistory.id.desc()))
    ranked = history_by_district(
        CaseStatusHistory.occurred_at.label("at"), CaseStatusHistory.from_status.label("from_status"), CaseStatusHistory.to_status.label("to_status"),
        CaseStatusHistory.actor_role.label("actor_role"), CaseStatusHistory.case_id.label("case_id"), CareGapCase.care_activity.label("care_activity"),
        district.label("district"), rn.label("rn"),
    ).subquery("ranked")  # fmt: skip
    recent_by_district: dict[str, list[dict[str, Any]]] = {}
    for r in session.execute(select(ranked).where(ranked.c.rn <= RECENT_CHANGES).order_by(ranked.c.at.desc())):
        recent_by_district.setdefault(_key(r.district), []).append(
            {"at": r.at.isoformat(), "from_status": r.from_status, "to_status": r.to_status, "actor_role": r.actor_role, "case_id": str(r.case_id), "care_activity": r.care_activity}
        )
    timings = _timings(session, district)
    return {
        "version": CORE_VERSION,
        "computed_at": now.isoformat(),
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "fingerprint": fp,
        "latest_dataset": session.scalar(select(Dataset.dataset_version).order_by(Dataset.created_at.desc()).limit(1)),
        "residents": residents,
        # [district, care_activity, status, priority, cases, ever_planned, ever_in_outreach]
        "cases": [[_key(d), a, s, p, n, pl, ou] for d, a, s, p, n, pl, ou in grid],
        "stages": stages,
        "evaluated": evaluated,
        "due": due,
        "due_by_activity": due_by_activity,
        "as_of": as_of.isoformat(),
        # [district, age band, sex, residents, cases, open, completed, in outreach, ever planned, due residents, ever attended cases]
        "cohorts": [[d, b, sx, *v] for (d, b, sx), v in sorted(cohorts.items())],
        "stocks": stocks,
        "funnel_previous": funnel_previous,
        "evidence": {"total": facts_total, "pending": facts_pending},
        "evidence_by_district": evidence_by_district,
        "trend_by_district": trend_by_district,
        "recent_by_district": recent_by_district,
        "timings": timings,
    }


def _funnel_previous(session: Session, district: Any, now: datetime) -> dict[str, Any]:
    """Funnel stage counts as they stood at the start of each comparison period, per district: cases opened, ever planned and
    ever in outreach (first time reached, from the append only history), cases completed at that moment, and residents
    evaluated. Residents due cannot be rebuilt for a past date (it needs the latest outcome as of then)."""
    h = CaseStatusHistory
    per_case = (
        select(
            CareGapCase.resident_id.label("resident_id"), district.label("district"), func.min(h.occurred_at).label("opened"),
            func.min(h.occurred_at).filter(h.to_status.in_(PLANNED_OR_LATER)).label("planned"),
            func.min(h.occurred_at).filter(h.to_status.in_(OUTREACH_OR_LATER)).label("outreach"),
        )
        .select_from(h).join(CareGapCase, CareGapCase.id == h.case_id).join(Resident, Resident.id == CareGapCase.resident_id)
        .group_by(h.case_id, CareGapCase.resident_id, district)
        .subquery("per_case_first")
    )  # fmt: skip
    first_eval = select(EligibilityEvaluation.resident_id.label("resident_id"), func.min(EligibilityEvaluation.evaluated_at).label("first_at")).group_by(EligibilityEvaluation.resident_id).offset(0).subquery("first_eval")
    ats = {str(d): now - timedelta(days=d) for d in COMPARE_DAYS}
    cols: list[Any] = [per_case.c.district]
    for at in ats.values():
        for stage in ("opened", "planned", "outreach"):
            ts = per_case.c[stage]
            cols += [func.count(distinct(per_case.c.resident_id)).filter(ts <= at), func.count().filter(ts <= at)]
    out: dict[str, dict[str, Any]] = {key: {"as_of": at.isoformat(), "by_district": {}} for key, at in ats.items()}
    for row in session.execute(select(*cols).group_by(per_case.c.district)):
        for i, key in enumerate(ats):
            vals = row[1 + i * 6 : 7 + i * 6]
            out[key]["by_district"][_key(row[0])] = {"opened": [vals[0], vals[1]], "planned": [vals[2], vals[3]], "outreach": [vals[4], vals[5]]}
    for row in session.execute(select(district, *[func.count().filter(first_eval.c.first_at <= at) for at in ats.values()]).select_from(first_eval).join(Resident, Resident.id == first_eval.c.resident_id).group_by(district)):
        for i, key in enumerate(ats):
            out[key]["by_district"].setdefault(_key(row[0]), {})["evaluated"] = row[1 + i]
    return out


def _timings(session: Session, district: Any) -> dict[str, Any]:
    """Hours from a case first being planned to first attended (awaiting completion evidence) and to completed: exact
    medians and 90th percentiles overall, per district and per state (medians cannot be combined afterwards)."""
    gmap = district_map()
    state_sql = "CASE anticipa.residents.district_code " + " ".join(f"WHEN '{d['code']}' THEN '{d['state_code']}'" for d in gmap["districts"]) + " ELSE NULL END"
    state = literal_column(state_sql)  # codes come from the trusted config file
    h = CaseStatusHistory
    per_case = (
        select(
            func.min(h.occurred_at).filter(h.to_status == "planned").label("planned"),
            func.min(h.occurred_at).filter(h.to_status == "awaiting_completion_evidence").label("attended"),
            func.min(h.occurred_at).filter(h.to_status == "completed").label("closed"),
            district.label("district"), state.label("state"),
        )
        .select_from(h).join(CareGapCase, CareGapCase.id == h.case_id).join(Resident, Resident.id == CareGapCase.resident_id)
        .group_by(h.case_id, district, state)
        .subquery("per_case")
    )  # fmt: skip

    def hours(end: Any) -> Any:
        return extract("epoch", end - per_case.c.planned) / 3600.0

    def stats(end: Any) -> list[Any]:
        ok = (per_case.c.planned.is_not(None), end.is_not(None), end >= per_case.c.planned)
        return [func.count().filter(*ok), func.percentile_cont(0.5).within_group(hours(end)).filter(*ok), func.percentile_cont(0.9).within_group(hours(end)).filter(*ok)]

    out: dict[str, Any] = {"all": {}, "district": {}, "state": {}}
    for level, d, st, att_n, att_med, att_p90, cl_n, cl_med, cl_p90 in session.execute(
        select(func.grouping(per_case.c.district, per_case.c.state), per_case.c.district, per_case.c.state, *stats(per_case.c.attended), *stats(per_case.c.closed))
        .group_by(func.grouping_sets(tuple_(), tuple_(per_case.c.district), tuple_(per_case.c.state)))
    ):
        value = {
            "attended": [att_n, None if att_med is None else round(float(att_med), 2), None if att_p90 is None else round(float(att_p90), 2)],
            "closed": [cl_n, None if cl_med is None else round(float(cl_med), 2), None if cl_p90 is None else round(float(cl_p90), 2)],
        }
        if level == 3:
            out["all"] = value
        elif level == 1:
            out["district"][_key(d)] = value
        elif st is not None:
            out["state"][st] = value
    return out


# ------------------------------------------------------------------------------------------------ store / load
def refresh(sessions: Callable[[], Session], *, reason: str = "cli") -> dict[str, Any]:
    """Compute the core aggregates and store them (one row, upsert). Skips when another refresh holds the lock."""
    with sessions() as session:
        if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK}):
            return {"skipped": "another refresh is running"}
        core = compute_core(session)
        stmt = pg_insert(AnalyticsSnapshot).values(
            key=CORE_KEY, payload=core, computed_at=datetime.fromisoformat(core["computed_at"]), duration_ms=core["duration_ms"], refreshed_by=reason
        )
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={"payload": stmt.excluded.payload, "computed_at": stmt.excluded.computed_at, "duration_ms": stmt.excluded.duration_ms,
                      "refreshed_by": stmt.excluded.refreshed_by},
            )
        )  # fmt: skip
        session.commit()
    from anticipa.analytics import service  # local import: service imports this module

    service.clear_cache()
    return {"refreshed": True, "reason": reason, "duration_ms": core["duration_ms"], "computed_at": core["computed_at"],
            "residents": sum(core["residents"].values()), "case_rows": len(core["cases"])}  # fmt: skip


def _read(session: Session) -> tuple[dict[str, Any], datetime] | None:
    """The stored core and its time; None when absent or when the table does not exist yet (before migration 0008)."""
    try:
        with session.begin_nested():  # a savepoint: a missing table must not poison the request transaction
            row = session.execute(select(AnalyticsSnapshot.payload, AnalyticsSnapshot.computed_at).where(AnalyticsSnapshot.key == CORE_KEY)).first()
    except DBAPIError:
        return None
    return None if row is None or row[0].get("version") != CORE_VERSION else (row[0], row[1])


_REFRESHING = threading.Lock()
_LAST_AUTO = 0.0


def _background_refresh(sessions: Callable[[], Session], reason: str) -> None:
    global _LAST_AUTO
    if time.monotonic() - _LAST_AUTO < AUTO_REFRESH_GAP_S or not _REFRESHING.acquire(blocking=False):
        return
    _LAST_AUTO = time.monotonic()

    def run() -> None:
        try:
            refresh(sessions, reason=reason)
        except Exception:
            log.exception("analytics background refresh failed")
        finally:
            _REFRESHING.release()

    threading.Thread(target=run, name="analytics-refresh", daemon=True).start()


def load_core(session: Session, sessions: Callable[[], Session] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(core, meta). Serves the stored snapshot; computes live when there is none. Never waits for a refresh."""
    stored = _read(session)
    now = datetime.now(UTC)
    if stored is None:
        if AUTO_REFRESH and sessions is not None:
            _background_refresh(sessions, "auto: no snapshot")
        core = compute_core(session, now)
        return core, {"source": "live", "computed_at": now, "age_seconds": 0}
    core, computed_at = stored
    age = round((now - computed_at).total_seconds())
    if AUTO_REFRESH and sessions is not None and age >= AUTO_REFRESH_GAP_S and fingerprint(session) != core.get("fingerprint"):
        _background_refresh(sessions, "auto: data changed")
    return core, {"source": "snapshot", "computed_at": computed_at, "age_seconds": age}


# ------------------------------------------------------------------------------------------------ hooks and CLI
def after_job(sessions: Callable[[], Session], job_type: str) -> None:
    """Worker hook: refresh after a successful job of an analytics relevant module (never raises, never fails the job)."""
    if not job_type.startswith(REFRESH_JOB_PREFIXES):
        return
    try:
        with sessions() as session:
            stored = _read(session)
        if stored is not None and (datetime.now(UTC) - stored[1]).total_seconds() < JOB_REFRESH_GAP_S:
            return
        refresh(sessions, reason=f"job: {job_type}")
    except Exception:
        log.exception("analytics refresh after job failed", extra={"job_type": job_type})


def main(argv: list[str] | None = None) -> int:
    from anticipa.db import get_sessionmaker

    p = argparse.ArgumentParser(prog="python -m anticipa.analytics", description="Precomputed analytics aggregates")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refresh", help="recompute and store the aggregates")
    sub.add_parser("status", help="show the stored snapshot's age and size")
    args = p.parse_args(argv)
    sessions = get_sessionmaker()
    if args.cmd == "refresh":
        print(json.dumps(refresh(sessions, reason="cli"), indent=2, default=str))
        return 0
    with sessions() as session:
        stored = _read(session)
    if stored is None:
        print(json.dumps({"snapshot": None}))
        return 1
    core, at = stored
    print(json.dumps({"computed_at": at.isoformat(), "age_seconds": round((datetime.now(UTC) - at).total_seconds()),
                      "duration_ms": core["duration_ms"], "residents": sum(core["residents"].values()), "case_rows": len(core["cases"])}, indent=2))  # fmt: skip
    return 0
