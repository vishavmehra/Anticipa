"""Map selection filters (integration, DB `anticipa_test`, rolled back): the Command Center, funnel and programmes accept
`district` or `state` and every widget follows; exact timings per scope; old snapshots degrade without inventing numbers."""
# ruff: noqa: E501

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from anticipa.analytics import places, service
from anticipa.caregaps.models import CaseStatusHistory
from anticipa.core.errors import NotFound, ValidationFailed
from anticipa.datafabric.models import Resident
from anticipa.planning.models import CapacitySlot

API = "/api/v1"
FULL_PATH = ["identified", "approved_for_planning", "planned", "outreach_pending", "outreach_completed",
             "appointment_scheduled", "awaiting_completion_evidence", "completed"]  # fmt: skip


def get(client, headers, path, status=200):
    r = client.get(f"{API}{path}", headers=headers)
    assert r.status_code == status, r.text
    return r.json()["data"] if status == 200 else r.json()["error"]


def kpis(data):
    return {k["key"]: k["value"] for k in data["kpis"]}


def seed(make_case):
    now = datetime.now(UTC)
    a = make_case("planned", district="D01", path=["identified", "approved_for_planning", "planned"], updated_at=now - timedelta(days=10))  # default priority: keeps updated_at
    b = make_case("completed", district="D01", path=FULL_PATH)
    c = make_case("identified", district="D02")
    d = make_case("outreach_pending", district="D05", priority="elevated", path=FULL_PATH[:4])
    e = make_case("cancelled", district="D09", path=["identified", "approved_for_planning", "planned", "cancelled"])
    return a, b, c, d, e


# ------------------------------------------------------------------------------------------------ places
def test_resolve_scope():
    assert places.resolve_scope() is None
    assert places.resolve_scope("d01") == ("D01",)
    assert places.resolve_scope(None, "MP") == ("D01", "D02", "D03", "D04") and places.resolve_scope(None, "in-up") == ("D07", "D08")
    assert places.resolve_scope("D05", "27") == ("D05",)
    assert places.resolve_scope(None, "32") == ()  # a state without a pilot district: an empty scope, not an error
    assert places.scope_info("D02", None) == {"district": "D02", "state": None, "districts": ["D02"], "label": "Indore"}
    assert places.scope_info(None, "23")["label"] == "Madhya Pradesh" and places.scope_info(None, None)["districts"] is None
    with pytest.raises(ValidationFailed):
        places.resolve_scope("D05", "23")
    with pytest.raises(NotFound):
        places.resolve_scope("D77")
    with pytest.raises(NotFound):
        places.resolve_scope(None, "ZZ")


# ------------------------------------------------------------------------------------------------ command center follows the selection
def test_district_filter_applies_to_every_widget(api_client, auth_headers, make_case):
    a, b, c, d, e = seed(make_case)
    h = auth_headers("observer")
    whole = get(api_client, h, "/analytics/command-center")
    assert kpis(whole)["care_gaps_total"] == 5 and whole["scope"] == {"district": None, "state": None, "districts": None, "label": "All districts"}
    bhopal = get(api_client, h, "/analytics/command-center?district=D01")
    assert bhopal["scope"] == {"district": "D01", "state": None, "districts": ["D01"], "label": "Bhopal"}
    k = kpis(bhopal)
    assert (k["residents_total"], k["care_gaps_total"], k["care_gaps_open"], k["gaps_closed"], k["in_outreach"], k["residents_with_open_gaps"]) == (2, 2, 1, 1, 0, 1)
    assert k["closure_rate"] == 0.5
    prio = {i["priority"]: i for i in bhopal["gaps_by_priority"]["items"]}
    assert prio["high"]["total"] == 0 and prio["routine"] == {"priority": "routine", "open": 1, "completed": 1, "total": 2}
    assert [r["district_code"] for r in bhopal["equity"]["districts"]] == ["D01"] and bhopal["equity"]["coverage"]["gap"] == 0.0
    pts = {p["date"]: p for p in bhopal["closure_trend"]["points"]}
    today = datetime.now(UTC).date().isoformat()
    assert pts[today]["closed"] == 1 and sum(p["opened"] for p in bhopal["closure_trend"]["points"]) == 2
    pune = get(api_client, h, "/analytics/command-center?district=D05")
    assert sum(p["closed"] for p in pune["closure_trend"]["points"]) == 0 and sum(p["opened"] for p in pune["closure_trend"]["points"]) == 1
    assert {i["resource_id"] for i in pune["recent_activity"]["items"]} == {str(d.id)}  # only that district's changes
    assert {i["priority"]: i["open"] for i in pune["gaps_by_priority"]["items"]}["elevated"] == 1
    assert {i["resource_id"] for i in bhopal["recent_activity"]["items"]} == {str(a.id), str(b.id)}


def test_state_filter_sums_its_districts(api_client, auth_headers, make_case):
    seed(make_case)
    h = auth_headers("observer")
    mp = get(api_client, h, "/analytics/command-center?state=MP")
    k = kpis(mp)
    assert (k["residents_total"], k["care_gaps_total"], k["gaps_closed"]) == (3, 3, 1) and mp["scope"]["state"] == "23" and mp["scope"]["label"] == "Madhya Pradesh"
    assert [r["district_code"] for r in mp["equity"]["districts"]] == ["D01", "D02"]
    assert kpis(get(api_client, h, "/analytics/command-center?state=23&district=D02"))["care_gaps_total"] == 1  # a district inside the state
    empty = get(api_client, h, "/analytics/command-center?state=32")  # a state with no pilot district: zeros, not an error
    assert kpis(empty)["residents_total"] == 0 and kpis(empty)["care_gaps_total"] == 0 and kpis(empty)["closure_rate"] is None and empty["equity"]["districts"] == []
    assert get(api_client, h, "/analytics/command-center?state=MP&district=D05", 422)["code"] == "VALIDATION_ERROR"
    assert get(api_client, h, "/analytics/command-center?district=D77", 404)["code"] == "NOT_FOUND"
    assert get(api_client, h, "/analytics/command-center?state=ZZ", 404)["code"] == "NOT_FOUND"


def test_alerts_follow_the_selection(api_client, auth_headers, make_case, world, db_session):
    seed(make_case)
    world.clinic.district_code = "D05"
    now = datetime.now(UTC)
    db_session.add(CapacitySlot(clinic_id=world.clinic.id, clinic_service_id=world.service.id, start_time=now + timedelta(days=1), end_time=now + timedelta(days=1, minutes=30),
                                available_capacity=4, reserved_capacity=4, slot_status="open", source_type="manual"))  # fully booked in Pune
    db_session.flush()
    h = auth_headers("observer")

    def rules(path):
        return {r["key"]: r for r in get(api_client, h, path)["alert_rules"]}

    whole, bhopal, pune = rules("/analytics/command-center"), rules("/analytics/command-center?district=D01"), rules("/analytics/command-center?district=D05")
    assert whole["capacity_utilisation"]["status"] == "alert" and pune["capacity_utilisation"]["status"] == "alert" and bhopal["capacity_utilisation"]["status"] == "no_data"
    assert whole["outreach_backlog"]["value"] == 1 and bhopal["outreach_backlog"]["status"] == "alert" and pune["outreach_backlog"]["status"] == "ok"  # the stale planned case is in Bhopal
    assert "jobs_failed" not in rules("/analytics/command-center?district=D01") and "jobs_failed" not in whole  # observers never see jobs


def test_internal_viewers_get_audit_and_jobs_only_without_a_filter(api_client, auth_headers, make_case, db_session):
    from anticipa.jobs.models import ProcessingJob

    seed(make_case)
    db_session.add(ProcessingJob(job_type="test.failed", status="failed"))
    db_session.flush()
    ops = auth_headers("system_admin")
    assert "jobs_failed" in {r["key"] for r in get(api_client, ops, "/analytics/command-center")["alert_rules"]}
    scoped = get(api_client, ops, "/analytics/command-center?district=D01")
    assert "jobs_failed" not in {r["key"] for r in scoped["alert_rules"]} and {i["kind"] for i in scoped["recent_activity"]["items"]} == {"case"}


def test_limited_role_can_filter_too(api_client, auth_headers, make_case):
    seed(make_case)
    data = get(api_client, auth_headers("care_coordinator"), "/analytics/command-center?district=D01")
    assert data["limited"] is True and kpis(data)["care_gaps_total"] == 2 and data["equity"] is None and data["scope"]["label"] == "Bhopal"


def test_comparison_follows_the_selection(api_client, auth_headers, world, db_session):
    now = datetime.now(UTC)

    def case(district, *steps):
        c = world.case("planned")
        db_session.get(Resident, c.resident_id).district_code = district
        for frm, to, ago in steps:
            db_session.add(CaseStatusHistory(case_id=c.id, from_status=frm, to_status=to, occurred_at=now - timedelta(days=ago)))
        db_session.flush()
        return c

    case("D01", (None, "identified", 40), ("identified", "planned", 35))  # exists 30 days ago
    case("D02", (None, "identified", 5))  # opened since
    h = auth_headers("observer")
    d1 = get(api_client, h, "/analytics/command-center?compare=previous&days=30&district=D01")["comparison"]
    assert d1["available"] and d1["kpis"]["care_gaps_total"]["current"] == 1 and d1["kpis"]["care_gaps_total"]["previous"] == 1 and d1["flows"]["opened"]["current"] == 0
    d2 = get(api_client, h, "/analytics/command-center?compare=previous&days=30&district=D02")["comparison"]
    assert d2["kpis"]["care_gaps_total"]["current"] == 1 and d2["kpis"]["care_gaps_total"]["previous"] == 0 and d2["flows"]["opened"]["current"] == 1
    whole = get(api_client, h, "/analytics/command-center?compare=previous&days=30")["comparison"]
    assert whole["kpis"]["care_gaps_total"]["current"] == 2 and whole["kpis"]["care_gaps_total"]["previous"] == 1


# ------------------------------------------------------------------------------------------------ timings
def test_timings_are_exact_per_scope(api_client, auth_headers, world, db_session):
    now = datetime.now(UTC)

    def case(district, planned_h, attended_h, closed_h):
        c = world.case("completed")
        db_session.get(Resident, c.resident_id).district_code = district
        steps = [(None, "identified", planned_h + 5), ("identified", "planned", planned_h)]
        if attended_h is not None:
            steps.append(("planned", "awaiting_completion_evidence", attended_h))
        if closed_h is not None:
            steps.append(("awaiting_completion_evidence", "completed", closed_h))
        for frm, to, ago in steps:
            db_session.add(CaseStatusHistory(case_id=c.id, from_status=frm, to_status=to, occurred_at=now - timedelta(hours=ago)))
        db_session.flush()

    case("D01", 20, 18, 14)  # attended after 2 h, closed after 6 h
    case("D05", 30, 24, None)  # attended after 6 h, not closed
    case("D02", 10, None, None)  # planned only: counted in neither
    h = auth_headers("observer")
    whole = get(api_client, h, "/analytics/command-center")["timings"]
    assert whole["source"] == "simulated" and "hours" in whole["definition"].lower()
    assert whole["time_to_attend"] == {"cases": 2, "median_hours": 4.0, "p90_hours": 5.6, "median_days": round(4.0 / 24, 4), "p90_days": round(5.6 / 24, 4)}
    assert (whole["time_to_close"]["cases"], whole["time_to_close"]["median_hours"], whole["time_to_close"]["p90_hours"]) == (1, 6.0, 6.0)
    assert get(api_client, h, "/analytics/command-center?district=D01")["timings"]["time_to_attend"]["median_hours"] == 2.0
    assert get(api_client, h, "/analytics/command-center?district=D05")["timings"]["time_to_attend"]["median_hours"] == 6.0
    assert get(api_client, h, "/analytics/command-center?district=D05")["timings"]["time_to_close"]["cases"] == 0
    assert get(api_client, h, "/analytics/command-center?state=MP")["timings"]["time_to_attend"]["cases"] == 1  # D01 and D02 only
    assert get(api_client, h, "/analytics/command-center?state=MH")["timings"]["time_to_attend"]["median_hours"] == 6.0
    assert get(api_client, h, "/analytics/command-center?state=KA")["timings"]["time_to_attend"]["median_days"] is None


# ------------------------------------------------------------------------------------------------ funnel and programmes
def test_funnel_and_programmes_accept_the_filter(api_client, auth_headers, make_case):
    seed(make_case)
    make_case("completed", district="D02", path=FULL_PATH, activity="breast_cancer_screening")
    h = auth_headers("observer")
    f = {s["key"]: s for s in get(api_client, h, "/analytics/funnel?district=D01")["stages"]}
    assert (f["residents"]["count"], f["case_opened"]["count"], f["closed"]["count"]) == (2, 2, 1)
    mp = get(api_client, h, "/analytics/funnel?state=MP")
    assert {s["key"]: s["count"] for s in mp["stages"]}["case_opened"] == 4 and mp["scope"]["label"] == "Madhya Pradesh"
    prog = get(api_client, h, "/analytics/programmes?district=D02")
    assert {p["care_activity"]: p["cases_total"] for p in prog["programmes"]} == {"colorectal_cancer_screening": 1, "breast_cancer_screening": 1} and prog["population"] == 2
    assert prog["scope"]["district"] == "D02"
    assert get(api_client, h, "/analytics/programmes?state=KA")["programmes"][0]["cases_total"] == 1  # D09
    assert get(api_client, h, "/analytics/funnel?district=D77", 404)["code"] == "NOT_FOUND"
    assert get(api_client, h, "/analytics/programmes?state=MP&district=D05", 422)["code"] == "VALIDATION_ERROR"


# ------------------------------------------------------------------------------------------------ old snapshots
def test_scoped_core_never_invents_what_an_old_snapshot_lacks():
    old = {"residents": {"D01": 5, "D02": 3}, "stages": {}, "evaluated": {}, "due": {}, "cases": [["D01", "x", "planned", "routine", 2, 1, 0]], "due_by_activity": [],
           "trend": {"2026-11-01": [1, 2]}, "stocks": {"7": {"as_of": "t", "statuses": {"planned": 2}}}, "recent_changes": [{"at": "2026-11-01T00:00:00+00:00"}],
           "evidence": {"total": 9, "pending": 3}}  # a snapshot from before the per district parts
    whole = service.scoped_core(old, None)
    assert whole["trend"] == {"2026-11-01": [1, 2]} and whole["stocks"]["7"]["statuses"] == {"planned": 2} and whole["evidence"] == {"total": 9, "pending": 3}
    scoped = service.scoped_core(old, ("D01",))
    assert scoped["residents"] == {"D01": 5} and scoped["trend"] == {} and scoped["stocks"] == {} and scoped["recent_changes"] == [] and scoped["evidence"] == {"total": 0, "pending": 0}


def test_cache_keys_include_the_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "COMMAND_CENTER_TTL_S", 60.0)
    monkeypatch.setattr(service, "_command_center", lambda session, sessions, limited, internal, days, compare, scope, now: calls.append(scope["label"]) or {"n": len(calls)})
    service.command_center(None)
    service.command_center(None)
    service.command_center(None, district="D01")
    service.command_center(None, state="MP")
    service.command_center(None, state="MP")
    assert calls == ["All districts", "Bhopal", "Madhya Pradesh"]
