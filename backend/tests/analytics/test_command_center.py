"""Command Center + funnel analytics against the real database (integration, DB `anticipa_test`, rolled back):
KPI arithmetic, definitions and source labels on every metric, equity spread, closure trend, alerts from the documented
thresholds, empty-data behaviour, the limited (coordinator) view and funnel counts."""
# ruff: noqa: E501

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from anticipa.analytics import service
from anticipa.core.models import AuditEvent
from anticipa.datafabric.models import Resident
from anticipa.evidence.models import EvidenceFact
from anticipa.guidelines.models import EligibilityEvaluation
from anticipa.jobs.models import ProcessingJob
from anticipa.planning.models import CapacitySlot

API = "/api/v1"
FULL_PATH = ["identified", "approved_for_planning", "planned", "outreach_pending", "outreach_completed",
             "appointment_scheduled", "awaiting_completion_evidence", "completed"]  # fmt: skip


def get(client, headers, path):
    r = client.get(f"{API}{path}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def kpis(data):
    return {k["key"]: k for k in data["kpis"]}


def alert_rules(data):
    return {r["key"]: r for r in data["alert_rules"]}


def assert_labelled(items):
    for m in items:
        assert m["definition"] and m["source"] in service.SOURCES, m


# ------------------------------------------------------------------------------------------------ empty database
def test_empty_data_shapes(api_client, auth_headers, db_session):
    data = get(api_client, auth_headers("observer"), "/analytics/command-center")
    k = kpis(data)
    assert k["care_gaps_total"]["value"] == 0 and k["gaps_closed"]["value"] == 0 and k["in_outreach"]["value"] == 0
    assert k["closure_rate"]["value"] is None and k["closure_rate"]["denominator"] == 0
    assert k["residents_total"]["value"] == db_session.scalar(select(func.count()).select_from(Resident))
    assert data["closure_trend"]["points"] == []
    assert data["equity"]["districts"] == [] and data["equity"]["coverage"] == {"min": None, "max": None, "gap": None}
    assert [p["priority"] for p in data["gaps_by_priority"]["items"]] == ["high", "elevated", "routine"]
    rules = alert_rules(data)
    assert set(rules) == {"capacity_utilisation", "outreach_backlog", "evidence_review_share"}  # jobs are internal: no jobs rule
    ops = alert_rules(get(api_client, auth_headers("system_admin"), "/analytics/command-center"))
    assert set(ops) == {"capacity_utilisation", "outreach_backlog", "evidence_review_share", "jobs_failed"}
    assert rules["capacity_utilisation"]["status"] == "no_data" and rules["evidence_review_share"]["status"] == "no_data"
    assert rules["outreach_backlog"]["status"] == "ok"
    assert all(a["status"] == "alert" for a in data["alerts"])
    assert data["restricted"] == [] and isinstance(data["recent_activity"]["items"], list)


def test_empty_funnel(api_client, auth_headers, db_session):
    data = get(api_client, auth_headers("observer"), "/analytics/funnel")
    stages = {s["key"]: s for s in data["stages"]}
    assert list(stages) == ["residents", "evaluated", "due", "case_opened", "planned", "in_outreach", "closed"]
    assert all(s["count"] == 0 for k, s in stages.items() if k != "residents")
    assert stages["evaluated"]["conversion_from_previous"] is None and stages["residents"]["conversion_from_previous"] is None
    assert_labelled(data["stages"])
    assert [i["cases"] for i in data["leakage"]["items"]] == [0, 0, 0]


# ------------------------------------------------------------------------------------------------ KPIs, priority, equity, trend
def seed_cases(make_case):
    now = datetime.now(UTC)
    a = make_case("planned", district="D1", priority="high", path=["identified", "approved_for_planning", "planned"])
    b = make_case("completed", district="D1", path=FULL_PATH, at=now)
    c = make_case("outreach_pending", district="D2", priority="elevated", path=FULL_PATH[:4])
    d = make_case("identified", district="D2")
    e = make_case("cancelled", district="D2", path=["identified", "approved_for_planning", "planned", "cancelled"])
    return a, b, c, d, e


def test_command_center_numbers(api_client, auth_headers, make_case):
    seed_cases(make_case)
    data = get(api_client, auth_headers("observer"), "/analytics/command-center")
    k = kpis(data)
    assert_labelled(data["kpis"])
    assert k["care_gaps_total"]["value"] == 5
    assert k["care_gaps_open"]["value"] == 3  # planned, outreach_pending, identified
    assert k["residents_with_open_gaps"]["value"] == 3
    assert k["gaps_closed"]["value"] == 1 and k["gaps_closed"]["source"] == "simulated"
    assert k["closure_rate"]["value"] == 0.2 and (k["closure_rate"]["numerator"], k["closure_rate"]["denominator"]) == (1, 5)
    assert k["in_outreach"]["value"] == 1
    prio = {i["priority"]: i for i in data["gaps_by_priority"]["items"]}
    assert prio["high"] == {"priority": "high", "open": 1, "completed": 0, "total": 1}
    assert prio["elevated"]["open"] == 1 and prio["routine"] == {"priority": "routine", "open": 1, "completed": 1, "total": 3}


def test_equity_by_district_uses_history_for_coverage(api_client, auth_headers, make_case):
    seed_cases(make_case)
    eq = get(api_client, auth_headers("observer"), "/analytics/command-center")["equity"]
    rows = {r["district_code"]: r for r in eq["districts"]}
    assert (rows["D1"]["cases"], rows["D1"]["planned"], rows["D1"]["completed"]) == (2, 2, 1)
    assert rows["D1"]["coverage"] == 1.0 and rows["D1"]["closure_rate"] == 0.5
    # D2: outreach_pending (reached planning), identified (not), cancelled-after-planned (history says it was planned)
    assert (rows["D2"]["cases"], rows["D2"]["planned"], rows["D2"]["completed"]) == (3, 2, 0)
    assert rows["D2"]["coverage"] == round(2 / 3, 4)
    assert eq["coverage"]["min"]["district_code"] == "D2" and eq["coverage"]["max"]["district_code"] == "D1"
    assert eq["coverage"]["gap"] == round(1 - 2 / 3, 4)
    assert eq["closure_rate"]["gap"] == 0.5 and eq["definition"] and eq["source"] == "simulated"


def test_closure_trend_is_dense_and_counts_by_day(api_client, auth_headers, make_case):
    now = datetime.now(UTC)
    make_case("completed", path=FULL_PATH, at=now)
    make_case("completed", path=FULL_PATH, at=now - timedelta(days=3))
    make_case("identified", at=now - timedelta(days=40))  # outside a 30-day window
    trend = get(api_client, auth_headers("observer"), "/analytics/command-center?days=30")["closure_trend"]
    pts = {p["date"]: p for p in trend["points"]}
    assert len(trend["points"]) == 30 and trend["source"] == "simulated" and trend["definition"]
    assert pts[now.date().isoformat()]["closed"] == 1
    assert pts[(now - timedelta(days=3)).date().isoformat()]["closed"] == 1
    assert sum(p["closed"] for p in trend["points"]) == 2
    assert sum(p["opened"] for p in trend["points"]) == 2  # the 40-day-old case is outside the window


# ------------------------------------------------------------------------------------------------ alerts
def add_slot(world, db_session, start, available, reserved, status="open"):
    db_session.add(
        CapacitySlot(clinic_id=world.clinic.id, clinic_service_id=world.service.id, start_time=start, end_time=start + timedelta(minutes=30),
                     available_capacity=available, reserved_capacity=reserved, slot_status=status, source_type="manual")
    )  # fmt: skip
    db_session.flush()


def test_capacity_alert_threshold_window_and_severity(api_client, auth_headers, world, db_session):
    h, now = auth_headers("observer"), datetime.now(UTC)
    add_slot(world, db_session, now + timedelta(days=1), 10, 8)  # 80%: below the 90% threshold
    add_slot(world, db_session, now + timedelta(days=30), 10, 10)  # outside the 7-day window
    add_slot(world, db_session, now + timedelta(days=2), 100, 100, status="closed")  # closed slots do not count
    rule = alert_rules(get(api_client, h, "/analytics/command-center"))["capacity_utilisation"]
    assert rule["status"] == "ok" and rule["value"] == 0.8 and rule["threshold"] == 0.9 and rule["severity"] is None
    add_slot(world, db_session, now + timedelta(days=3), 10, 10)  # 18 of 20 = 90%
    data = get(api_client, h, "/analytics/command-center")
    rule = alert_rules(data)["capacity_utilisation"]
    assert rule["status"] == "alert" and rule["severity"] == "warning" and rule["value"] == 0.9
    assert rule["seats"] == 20 and rule["reserved_seats"] == 18 and rule["districts_at_or_over_threshold"] == ["D1"]
    assert "capacity_utilisation" in [a["key"] for a in data["alerts"]]
    add_slot(world, db_session, now + timedelta(days=4), 10, 10)
    assert alert_rules(get(api_client, h, "/analytics/command-center"))["capacity_utilisation"]["value"] == round(28 / 30, 4)
    add_slot(world, db_session, now + timedelta(days=5), 20, 0)  # utilisation falls again
    assert alert_rules(get(api_client, h, "/analytics/command-center"))["capacity_utilisation"]["status"] == "ok"


def test_outreach_backlog_alert(api_client, auth_headers, make_case):
    old = datetime.now(UTC) - timedelta(days=10)
    make_case("planned", updated_at=old)
    make_case("planned")  # fresh
    make_case("outreach_pending", updated_at=old)  # only planned cases (no outreach yet) are backlog
    rule = alert_rules(get(api_client, auth_headers("observer"), "/analytics/command-center"))["outreach_backlog"]
    assert rule["status"] == "alert" and rule["value"] == 1 and rule["max_age_days"] == 7 and rule["severity"] == "warning"
    assert rule["oldest_status_change_at"] is not None


def test_evidence_review_share_alert_needs_a_minimum_sample(api_client, auth_headers, seed, db_session):
    h, r = auth_headers("observer"), seed.resident()
    facts = [seed.fact(r, fact_type=f"synthetic_{i}") for i in range(10)]
    for f in facts[:5]:  # 50% pending but only 10 facts: not meaningful
        f.validation_status, f.review_status = "requires_review", "pending"
    db_session.flush()
    rule = alert_rules(get(api_client, h, "/analytics/command-center"))["evidence_review_share"]
    assert rule["status"] == "no_data" and rule["pending_facts"] == 5 and rule["total_facts"] == 10
    more = [seed.fact(r, fact_type=f"synthetic_more_{i}") for i in range(10)]  # 20 facts, 5 pending = 25%
    assert more
    rule = alert_rules(get(api_client, h, "/analytics/command-center"))["evidence_review_share"]
    assert rule["status"] == "alert" and rule["value"] == 0.25 and rule["threshold"] == 0.1
    assert db_session.scalar(select(func.count()).select_from(EvidenceFact)) == 20


def test_failed_jobs_alert_is_internal_to_system_admin(api_client, auth_headers, db_session):
    db_session.add(ProcessingJob(job_type="test.failed", status="failed", error_summary="boom"))
    db_session.flush()
    data = get(api_client, auth_headers("system_admin"), "/analytics/command-center")
    rule = alert_rules(data)["jobs_failed"]
    assert rule["status"] == "alert" and rule["value"] >= 1 and rule["source"] == "live"
    assert any(i["kind"] == "job" and i["title"] == "test.failed" for i in data["recent_activity"]["items"])
    for role in ("observer", "population_admin", "clinical_reviewer"):  # nobody else sees jobs, failed or not
        other = get(api_client, auth_headers(role), "/analytics/command-center")
        assert "jobs_failed" not in alert_rules(other) and "jobs_failed" not in [a["key"] for a in other["alerts"]]
        assert "test.failed" not in str(other)


# ------------------------------------------------------------------------------------------------ recent activity / limited view
def test_recent_activity_shows_product_changes_only_unless_system_admin(api_client, auth_headers, make_case, db_session):
    actor = uuid.uuid4()
    make_case("planned", path=["identified", "approved_for_planning", "planned"])
    db_session.add_all([
        AuditEvent(action="care_gap.close", resource_type="care_gap_case", resource_id="x1", outcome="success", actor_id=actor,
                   actor_role="care_coordinator", request_id="req-secret", details={"note": "secret"}),
        AuditEvent(action="resident.read", resource_type="resident", resource_id="x2", outcome="success", actor_id=actor),
        AuditEvent(action="evaluation.view", resource_type="evaluation_run", resource_id="x3", outcome="success", actor_id=actor),
        ProcessingJob(job_type="test.job", status="running"),
    ])  # fmt: skip
    db_session.flush()
    for role in ("observer", "population_admin", "clinical_reviewer"):
        items = get(api_client, auth_headers(role), "/analytics/command-center")["recent_activity"]["items"]
        assert items and {i["kind"] for i in items} == {"case"}, role  # no audit events, no jobs for product users or judges
        assert [i["title"] for i in items][:1] == ["care_gap.planned"] and {"care_gap.opened"} <= {i["title"] for i in items}
        assert "care_gap.close" not in str(items) and "test.job" not in str(items) and str(actor) not in str(items)
    items = get(api_client, auth_headers("system_admin"), "/analytics/command-center")["recent_activity"]["items"]
    titles = [i["title"] for i in items]
    assert {"care_gap.close", "test.job", "care_gap.planned"} <= set(titles)
    assert "resident.read" not in titles and "evaluation.view" not in titles  # read events stay out of the feed
    item = next(i for i in items if i["title"] == "care_gap.close")
    assert item["actor_role"] == "care_coordinator" and item["outcome"] == "success"
    assert not ({"actor_id", "request_id", "details"} & set(item))
    assert str(actor) not in str(items) and "secret" not in str(items)


def test_limited_role_gets_no_equity_or_activity(api_client, auth_headers, make_case):
    seed_cases(make_case)
    data = get(api_client, auth_headers("care_coordinator"), "/analytics/command-center")
    assert data["limited"] is True and data["equity"] is None and data["recent_activity"] is None
    assert data["restricted"] == ["equity", "recent_activity"]
    assert kpis(data)["care_gaps_total"]["value"] == 5 and data["closure_trend"]["points"]


# ------------------------------------------------------------------------------------------------ funnel
def test_funnel_stages_are_cumulative_over_history(api_client, auth_headers, make_case):
    seed_cases(make_case)
    data = get(api_client, auth_headers("observer"), "/analytics/funnel")
    s = {x["key"]: x for x in data["stages"]}
    assert_labelled(data["stages"])
    assert (s["case_opened"]["count"], s["case_opened"]["cases"]) == (5, 5)
    assert s["planned"]["count"] == 4  # planned, completed, outreach_pending, and the cancelled one that was planned
    assert s["in_outreach"]["count"] == 2  # completed + outreach_pending
    assert s["closed"]["count"] == 1
    assert s["closed"]["conversion_from_previous"] == 0.5 and s["planned"]["conversion_from_previous"] == 0.8
    leak = {i["status"]: i["cases"] for i in data["leakage"]["items"]}
    assert leak == {"closed_not_completed": 0, "cancelled": 1, "escalated": 0}


def test_funnel_due_counts_latest_evaluation_only(api_client, auth_headers, seed, seed_module, db_session):
    from datetime import date

    version, _ = seed.approved_version()
    residents = [seed.resident(), seed.resident(), seed.resident(covered=False)]
    seed_module.run_evaluation(db_session, [version], date(2026, 11, 8), resident_ids=[r.id for r in residents])
    evals = db_session.execute(select(EligibilityEvaluation)).scalars().all()
    due = {e.resident_id for e in evals if e.outcome in ("eligible", "requires_clinical_review")}
    assert len(evals) == 3 and due
    h = auth_headers("observer")
    s = {x["key"]: x for x in get(api_client, h, "/analytics/funnel")["stages"]}
    assert s["evaluated"]["count"] == 3 and s["due"]["count"] == len(due)
    # a newer evaluation of the same guideline version supersedes the older one
    first = next(e for e in evals if e.resident_id in due)
    db_session.add(EligibilityEvaluation(
        resident_id=first.resident_id, guideline_version_id=first.guideline_version_id, outcome="not_eligible", outcome_class="ineligible",
        explanation={}, input_hash="x", evaluation_date=date(2026, 11, 9), evaluated_at=first.evaluated_at + timedelta(seconds=5),
        execution_version="test"))  # fmt: skip
    db_session.flush()
    s = {x["key"]: x for x in get(api_client, h, "/analytics/funnel")["stages"]}
    assert s["evaluated"]["count"] == 3 and s["due"]["count"] == len(due) - 1

