"""UI data gap endpoints (integration, DB `anticipa_test`, rolled back): period on period comparison, due residents per
district, the alert count badge, programme screening coverage, the age and sex population explorer, the scope list, and the
resident profile and evaluations views. Real data only: anything that does not exist is reported as not available."""
# ruff: noqa: E501

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, text

from anticipa.analytics import snapshot
from anticipa.caregaps.models import CaseStatusHistory
from anticipa.core.models import AuditEvent
from anticipa.datafabric.models import Resident
from anticipa.guidelines.models import EligibilityEvaluation
from anticipa.planning.models import CapacitySlot, Clinic

API = "/api/v1"


def get(client, headers, path, status=200):
    r = client.get(f"{API}{path}", headers=headers)
    assert r.status_code == status, r.text
    return r.json()["data"] if status == 200 else r.json()["error"]


def timeline(db_session, case, *steps):
    """History rows with explicit ages in days: steps are (from_status, to_status, days_ago)."""
    now = datetime.now(UTC)
    for frm, to, ago in steps:
        db_session.add(CaseStatusHistory(case_id=case.id, from_status=frm, to_status=to, occurred_at=now - timedelta(days=ago)))
    db_session.flush()


# ------------------------------------------------------------------------------------------------ due per district
def test_equity_rows_carry_due_residents(api_client, auth_headers, seed, seed_module, db_session):
    version, _ = seed.approved_version()
    residents = [seed.resident(district="D01"), seed.resident(district="D01"), seed.resident(district="D02")]
    seed_module.run_evaluation(db_session, [version], date(2026, 11, 8), resident_ids=[r.id for r in residents])
    due = {e.resident_id for e in db_session.execute(select(EligibilityEvaluation)).scalars() if e.outcome in ("eligible", "requires_clinical_review")}
    eq = get(api_client, auth_headers("observer"), "/analytics/command-center")["equity"]
    rows = {r["district_code"]: r for r in eq["districts"]}
    assert rows["D01"]["due_residents"] == len(due & {residents[0].id, residents[1].id}) and rows["D01"]["residents"] == 2
    assert rows["D01"]["due_share"] == round(rows["D01"]["due_residents"] / 2, 4) and rows["D02"]["due_residents"] == len(due & {residents[2].id})
    assert due and "due_residents" in eq["definition"]


# ------------------------------------------------------------------------------------------------ period on period
def test_compare_previous_rebuilds_the_past_from_the_status_history(api_client, auth_headers, world, db_session):
    a, b, c = world.case("completed"), world.case("identified"), world.case("completed")
    timeline(db_session, a, (None, "identified", 40), ("identified", "planned", 35), ("planned", "completed", 3))  # open 30 days ago, closed since
    timeline(db_session, b, (None, "identified", 10))  # opened inside the window
    timeline(db_session, c, (None, "identified", 50), ("identified", "completed", 45))  # closed before the window
    data = get(api_client, auth_headers("observer"), "/analytics/command-center?compare=previous&days=30")
    cmp = data["comparison"]
    assert cmp["available"] is True and cmp["window_days"] == 30 and cmp["source"] == "simulated" and cmp["definition"]
    k = cmp["kpis"]
    assert (k["care_gaps_total"]["current"], k["care_gaps_total"]["previous"], k["care_gaps_total"]["delta"]) == (3, 2, 1)
    assert (k["care_gaps_open"]["current"], k["care_gaps_open"]["previous"], k["care_gaps_open"]["delta"]) == (1, 1, 0)
    assert (k["gaps_closed"]["current"], k["gaps_closed"]["previous"], k["gaps_closed"]["delta"]) == (2, 1, 1)
    assert k["closure_rate"]["current"] == round(2 / 3, 4) and k["closure_rate"]["previous"] == 0.5 and k["closure_rate"]["delta_pp"] == 16.7
    assert k["in_outreach"]["delta"] == 0
    closed, opened = cmp["flows"]["closed"], cmp["flows"]["opened"]
    assert (closed["current"], closed["previous"], closed["delta"], closed["delta_pct"]) == (1, 1, 0, 0.0)
    assert (opened["current"], opened["previous"], opened["delta"], opened["delta_pct"]) == (1, 2, -1, -50.0)
    assert set(cmp["not_available"]) == {"residents_total", "residents_with_open_gaps"}
    assert "comparison" not in get(api_client, auth_headers("observer"), "/analytics/command-center")  # opt in


def test_compare_previous_validates_the_period_and_survives_an_old_snapshot(api_client, auth_headers, world, sessions, db_session):
    h = auth_headers("observer")
    assert get(api_client, h, "/analytics/command-center?compare=previous&days=10", 422)["code"] == "VALIDATION_ERROR"
    assert get(api_client, h, "/analytics/command-center?compare=next&days=30", 422)["code"] == "VALIDATION_ERROR"
    world.case("planned")
    snapshot.refresh(sessions, reason="test")
    db_session.execute(text("UPDATE analytics_snapshots SET payload = payload - 'stocks'"))  # a snapshot from before this feature
    cmp = get(api_client, h, "/analytics/command-center?compare=previous&days=7")["comparison"]
    assert cmp["available"] is False and "refresh" in cmp["reason"]


# ------------------------------------------------------------------------------------------------ alert badge
def test_alerts_summary_counts_the_firing_rules(api_client, auth_headers, world, make_case, db_session):
    now = datetime.now(UTC)
    assert get(api_client, auth_headers("observer"), "/analytics/alerts/summary")["alerts_active"] == 0
    db_session.add(CapacitySlot(clinic_id=world.clinic.id, clinic_service_id=world.service.id, start_time=now + timedelta(days=1), end_time=now + timedelta(days=1, minutes=30),
                                available_capacity=4, reserved_capacity=4, slot_status="open", source_type="manual"))  # fully booked: critical
    make_case("planned", updated_at=now - timedelta(days=10))  # an outreach backlog
    data = get(api_client, auth_headers("observer"), "/analytics/alerts/summary")
    assert data["alerts_active"] == 2 and data["by_severity"] == {"critical": 1, "warning": 1}
    assert {i["key"] for i in data["items"]} == {"capacity_utilisation", "outreach_backlog"} and data["definition"] and data["snapshot"]["source"]
    from anticipa.jobs.models import ProcessingJob

    db_session.add(ProcessingJob(job_type="test.failed", status="failed"))
    db_session.flush()
    assert get(api_client, auth_headers("observer"), "/analytics/alerts/summary")["alerts_active"] == 2  # jobs are internal
    ops = get(api_client, auth_headers("system_admin"), "/analytics/alerts/summary")
    assert ops["alerts_active"] == 3 and "jobs_failed" in {i["key"] for i in ops["items"]}
    assert get(api_client, auth_headers("care_coordinator"), "/analytics/alerts/summary")["alerts_active"] == 2  # the limited role may read the badge


# ------------------------------------------------------------------------------------------------ programme coverage
def test_programmes_report_screening_coverage(api_client, auth_headers, seed, seed_module, db_session):
    version, _ = seed.approved_version(care_activity="coverage_demo")
    due_resident, done_resident = seed.resident(district="D01"), seed.resident(district="D01")
    seed.fact(done_resident, category="procedure", fact_type="synthetic_screening", subject_type="resident", relationship=None, event_date="2025-06-01")
    seed_module.run_evaluation(db_session, [version], date(2026, 11, 8), resident_ids=[due_resident.id, done_resident.id])
    outcomes = {e.resident_id: e.outcome for e in db_session.execute(select(EligibilityEvaluation)).scalars()}
    up, due = sum(o == "already_completed" for o in outcomes.values()), sum(o in ("eligible", "requires_clinical_review") for o in outcomes.values())
    assert up >= 1 and due >= 1  # the fixture really produces both
    prog = {p["care_activity"]: p for p in get(api_client, auth_headers("observer"), "/analytics/programmes")["programmes"]}["coverage_demo"]
    assert (prog["up_to_date_residents"], prog["due_residents"]) == (up, due) and prog["screening_coverage"] == round(up / (up + due), 4)
    district = get(api_client, auth_headers("observer"), "/analytics/geography?level=district&code=D01")
    assert district["area"]["programmes"][0]["screening_coverage"] == prog["screening_coverage"]
    assert {"up_to_date_residents", "screening_coverage"} <= set(district["definitions"])


# ------------------------------------------------------------------------------------------------ population explorer
def seed_cohorts(seed, make_case):
    seed.resident(birth=date(1990, 1, 1), sex="female", district="D01", covered=False)
    seed.resident(birth=date(1990, 6, 1), sex="male", district="D01", covered=False)
    seed.resident(birth=date(1950, 1, 1), sex="female", district="D02", covered=False)
    seed.resident(birth=None, sex="female", district="D02", covered=False)
    make_case("planned", district="D01")  # a World resident: born 1970, female, so 50 to 59 years old
    make_case("completed", district="D01", path=["identified", "planned", "completed"])


def test_explorer_slices_by_district_age_band_and_sex(api_client, auth_headers, seed, make_case):
    seed_cohorts(seed, make_case)
    h = auth_headers("observer")
    allp = get(api_client, h, "/analytics/population-explorer")
    assert allp["available"] is True and allp["slice"]["population"] == 6 and allp["filters"] == {"district": None, "age_band": None, "sex": None}
    cells = {(c["age_band"], c["sex"]): c for c in allp["pyramid"]}
    assert cells[("30-39", "female")]["residents"] == 1 and cells[("30-39", "male")]["residents"] == 1 and cells[("70-79", "female")]["residents"] == 1
    assert cells[("unknown", "female")]["residents"] == 1  # no birth date: kept visible, not dropped
    assert cells[("50-59", "female")] == {"age_band": "50-59", "sex": "female", "residents": 2, "open_care_gaps": 1, "due_residents": 0}
    assert sum(c["residents"] for c in allp["pyramid"]) == 6 and allp["not_available"]["risk_stratum"] and allp["not_available"]["rural_urban"]
    d1 = get(api_client, h, "/analytics/population-explorer?district=d01")
    assert d1["slice"]["population"] == 4 and d1["slice"]["open_care_gaps"] == 1 and d1["slice"]["completed"] == 1 and d1["slice"]["closure_rate"] == 0.5
    assert d1["slice"]["gaps_per_100"] == 25.0 and sum(c["residents"] for c in d1["pyramid"]) == 4
    assert d1["slice"]["attended"] == 1  # the completed case in D01 was attended
    multi = get(api_client, h, "/analytics/population-explorer?district=D01&age_band=30-39,50-59")  # several bands at once
    assert multi["slice"]["population"] == 4 and multi["filters"]["age_band"] == "30-39,50-59"
    older = get(api_client, h, "/analytics/population-explorer?age_band=50-59,60-69,70-79,80%2B&sex=female")  # ages 50 and over, women
    assert older["slice"]["population"] == 3 and older["filters"]["sex"] == "female"  # two World residents and the 70 to 79 year old
    assert get(api_client, h, "/analytics/population-explorer?age_band=30-39,999", 422)["code"] == "VALIDATION_ERROR"
    both = get(api_client, h, "/analytics/population-explorer?district=D01&age_band=30-39&sex=male")
    assert both["slice"]["population"] == 1 and sum(c["residents"] for c in both["pyramid"]) == 4  # the pyramid keeps its shape while the slice narrows
    assert [d["code"] for d in both["options"]["districts"]][:2] == ["D01", "D02"] and "80+" in both["options"]["age_bands"]
    assert set(both["definitions"]) >= {"slice", "pyramid", "population", "closure_rate"}


def test_explorer_validation(api_client, auth_headers):
    h = auth_headers("observer")
    assert get(api_client, h, "/analytics/population-explorer?district=D77", 404)["code"] == "NOT_FOUND"
    assert get(api_client, h, "/analytics/population-explorer?age_band=200", 422)["code"] == "VALIDATION_ERROR"
    assert get(api_client, h, "/analytics/population-explorer?sex=robot", 422)["code"] == "VALIDATION_ERROR"
    empty = get(api_client, h, "/analytics/population-explorer")  # no residents loaded: reported, not invented
    assert empty["available"] is False and empty["slice"] is None and empty["pyramid"] == [] and empty["reason"]


# ------------------------------------------------------------------------------------------------ scope
def test_scope_lists_the_places_served(api_client, auth_headers, db_session):
    db_session.add_all([Clinic(clinic_key=f"scope-{uuid.uuid4().hex[:6]}", name="Scope test clinic (simulated)", district_code="D01") for _ in range(2)])
    db_session.flush()
    data = get(api_client, auth_headers("care_coordinator"), "/meta/scope")  # the shell shows it to everyone with a view
    assert data["country"] == {"code": "IN", "name": "India"} and [s["name"] for s in data["states"]] == ["Uttar Pradesh", "Madhya Pradesh", "Maharashtra", "Karnataka"]
    assert data["totals"]["districts"] == 10 and data["totals"]["states_with_coverage"] == 4 and data["totals"]["states_listed"] == 36
    d1 = next(d for d in data["districts"] if d["code"] == "D01")
    assert (d1["name"], d1["state"], d1["facilities"]) == ("Bhopal", "Madhya Pradesh", 2) and data["totals"]["facilities"] >= 2
    assert data["facilities_source"] == "simulated" and "households_total" in data["not_available"] and data["definition"]


# ------------------------------------------------------------------------------------------------ profile and evaluations
def evaluated_resident(seed, seed_module, db_session, district="D02"):
    version, _ = seed.approved_version(care_activity="profile_demo")
    resident = seed.resident(district=district)
    seed_module.run_evaluation(db_session, [version], date(2026, 11, 8), resident_ids=[resident.id])
    return resident


def test_profile_gives_place_and_programme_coverage(api_client, auth_headers, seed, seed_module, db_session):
    resident = evaluated_resident(seed, seed_module, db_session)
    data = get(api_client, auth_headers("observer"), f"/residents/{resident.id}/profile")
    assert data["resident"]["id"] == str(resident.id) and data["place"]["city"] == "Indore" and data["place"]["state"] == "Madhya Pradesh" and data["place"]["district_code"] == "D02"
    prog = data["coverage"]["programmes"]
    assert [p["care_activity"] for p in prog] == ["profile_demo"] and prog[0]["status"] in ("due", "needs_review") and prog[0]["status_label"]
    assert prog[0]["guideline"]["version"] == 1 and sum(data["coverage"]["counts"].values()) == 1 and data["coverage"]["definition"]
    assert set(data["not_available"]) == {"display_name", "photo", "risk_level", "risk_score", "household_size"} and data["restricted"] == []
    audited = db_session.execute(select(AuditEvent).where(AuditEvent.resource_id == str(resident.id), AuditEvent.action == "resident.read")).scalars().all()
    assert [a.details["view"] for a in audited] == ["profile"]
    assert get(api_client, auth_headers("observer"), f"/residents/{uuid.uuid4()}/profile", 404)["code"] == "NOT_FOUND"


def test_profile_of_an_unplaced_resident_has_no_place(api_client, auth_headers, seed):
    resident = seed.resident(district="D99")
    data = get(api_client, auth_headers("observer"), f"/residents/{resident.id}/profile")
    assert data["place"] is None and data["coverage"]["programmes"] == []


# ------------------------------------------------------------------------------------------------ whole note text
class FakeStore:
    def __init__(self, texts: dict[str, str]) -> None:
        self.texts = texts

    def read_text(self, key: str) -> str:
        if key not in self.texts:
            raise FileNotFoundError(key)
        return self.texts[key]


def test_document_text_returns_the_whole_note_with_evidence_spans(api_client, auth_headers, seed, monkeypatch, db_session):
    from anticipa.datafabric.models import Document
    from anticipa.evidence import service as evidence_service
    from anticipa.evidence.models import EvidenceSource

    resident, other = seed.resident(covered=False), seed.resident(covered=False)
    doc, foreign = seed.document(resident), seed.document(other)
    text = "Referral letter. Her mother was treated for breast cancer in 2015. No other history."
    quote = "Her mother was treated for breast cancer"
    start = text.index(quote)
    fact = seed.fact(resident, fact_type="breast_cancer", relationship="mother", quote=quote, document_id=doc.id)
    src = db_session.scalar(select(EvidenceSource).where(EvidenceSource.evidence_fact_id == fact.id))
    src.source_start_offset, src.source_end_offset = start, start + len(quote)
    db_session.get(Document, doc.id).language = "ar"
    db_session.flush()
    monkeypatch.setattr(evidence_service, "get_store", lambda: FakeStore({doc.storage_key: text}))
    h = auth_headers("observer")
    data = get(api_client, h, f"/residents/{resident.id}/documents/{doc.id}/text")
    assert data["text"] == text and data["characters"] == len(text) and data["direction"] == "rtl" and data["document"]["id"] == str(doc.id) and data["definition"]
    span = data["spans"][0]
    assert (span["fact_id"], span["start_offset"], span["end_offset"], span["verification_status"], span["quote"]) == (str(fact.id), start, start + len(quote), "verified_exact", quote)
    assert text[span["start_offset"] : span["end_offset"]] == quote  # the offsets point into the returned text
    assert span["fact_type"] == "breast_cancer" and span["validation_status"] == "accepted"
    audited = db_session.execute(select(AuditEvent).where(AuditEvent.resource_id == str(resident.id), AuditEvent.action == "resident.read")).scalars().all()
    assert [a.details["view"] for a in audited] == ["document"]
    # another resident's document, an unknown document and a missing file are all 404; a role without documents.view is 403
    assert get(api_client, h, f"/residents/{resident.id}/documents/{foreign.id}/text", 404)["code"] == "NOT_FOUND"
    assert get(api_client, h, f"/residents/{resident.id}/documents/{uuid.uuid4()}/text", 404)["code"] == "NOT_FOUND"
    monkeypatch.setattr(evidence_service, "get_store", lambda: FakeStore({}))
    assert "not stored" in get(api_client, h, f"/residents/{resident.id}/documents/{doc.id}/text", 404)["message"]
    assert get(api_client, auth_headers("care_coordinator"), f"/residents/{resident.id}/documents/{doc.id}/text", 403)["code"] == "FORBIDDEN"


# ------------------------------------------------------------------------------------------------ demand summary
def test_demand_summary_counts_residents_and_appointments_separately(api_client, auth_headers, make_case, db_session):
    from anticipa.caregaps.models import CareGapCase

    a = make_case("approved_for_planning", district="D01")
    make_case("awaiting_capacity", district="D01", priority="high", activity="breast_cancer_screening")
    make_case("approved_for_planning", district="D05")
    make_case("planned", district="D01")  # already planned: not waiting for a slot
    second = CareGapCase(resident_id=a.resident_id, evaluation_id=uuid.uuid4(), guideline_version_id=uuid.uuid4(), care_activity="cervical_cancer_screening",
                         status="approved_for_planning", priority="routine", source_outcome="eligible", synthetic_status="synthetic")
    db_session.add(second)  # the same resident needs a second appointment for another programme
    db_session.flush()
    h = auth_headers("observer")
    data = get(api_client, h, "/analytics/demand-summary")
    assert data["target_population"]["value"] == 3 and data["appointments_required"]["value"] == 4  # residents versus appointments
    assert data["target_population"]["definition"] and data["appointments_required"]["definition"]
    assert {p["care_activity"]: p["appointments_required"] for p in data["by_programme"]} == {"colorectal_cancer_screening": 2, "breast_cancer_screening": 1, "cervical_cancer_screening": 1}
    assert {p["priority"]: p["appointments_required"] for p in data["by_priority"]} == {"high": 1, "elevated": 0, "routine": 3}
    d1 = get(api_client, h, "/analytics/demand-summary?district=D01")
    assert (d1["target_population"]["value"], d1["appointments_required"]["value"], d1["scope"]["label"]) == (2, 3, "Bhopal")
    assert get(api_client, h, "/analytics/demand-summary?state=MH")["appointments_required"]["value"] == 1
    assert get(api_client, auth_headers("care_coordinator"), "/analytics/demand-summary", 403)["code"] == "FORBIDDEN"
    assert get(api_client, h, "/analytics/demand-summary?district=D77", 404)["code"] == "NOT_FOUND"


# ------------------------------------------------------------------------------------------------ schedule
def test_schedule_groups_one_day_of_slots_by_clinic_and_service(api_client, auth_headers, world, db_session):
    from anticipa.engagement.models import Appointment
    from anticipa.planning.models import ClinicService

    day = datetime.now(UTC).date() + timedelta(days=3)
    t0 = datetime(day.year, day.month, day.day, 9, tzinfo=UTC)
    other = Clinic(clinic_key=f"sched-{uuid.uuid4().hex[:6]}", name="Zeta clinic (simulated)", district_code="D05")
    db_session.add(other)
    db_session.flush()
    world.clinic.district_code = "D01"
    breast = ClinicService(clinic_id=world.clinic.id, service_key="breast_screening", service_name="Breast screening", appointment_duration_minutes=30)
    pune = ClinicService(clinic_id=other.id, service_key=world.service.service_key, service_name=world.service.service_name, appointment_duration_minutes=30)
    db_session.add_all([breast, pune])
    db_session.flush()

    def slot(clinic, service, hours, seats, reserved, status="open"):
        sl = CapacitySlot(clinic_id=clinic.id, clinic_service_id=service.id, start_time=t0 + timedelta(hours=hours), end_time=t0 + timedelta(hours=hours, minutes=30),
                          available_capacity=seats, reserved_capacity=reserved, slot_status=status, source_type="manual")
        db_session.add(sl)
        db_session.flush()
        return sl

    first = slot(world.clinic, world.service, 0, 4, 2)
    slot(world.clinic, world.service, 1, 6, 0)
    slot(world.clinic, breast, 2, 3, 3)
    slot(other, pune, 0, 5, 1)
    slot(world.clinic, world.service, 24, 9, 9)  # the next day
    slot(world.clinic, world.service, 3, 9, 9, status="closed")  # closed slots are not offered
    db_session.add(Appointment(care_gap_case_id=uuid.uuid4(), resident_id=uuid.uuid4(), clinic_id=world.clinic.id, clinic_service_id=world.service.id, capacity_slot_id=first.id,
                               planned_allocation_id=uuid.uuid4(), start_time=first.start_time, end_time=first.end_time, appointment_status="proposed"))
    db_session.flush()
    h = auth_headers("observer")
    data = get(api_client, h, f"/analytics/schedule?day={day.isoformat()}")
    assert data["totals"] == {"clinics": 2, "slots": 4, "seats": 18, "reserved": 6, "held_appointments": 1, "utilisation": round(6 / 18, 4)}
    assert data["day"] == day.isoformat() and data["source"] == "simulated" and data["definition"] and data["truncated"] is False
    by_service = {s["service_key"]: s for s in data["by_service"]}
    assert (by_service[world.service.service_key]["seats"], by_service[world.service.service_key]["reserved"]) == (15, 3) and by_service["breast_screening"]["utilisation"] == 1.0
    clinics = {c["name"]: c for c in data["clinics"]}
    mine = clinics[world.clinic.name]
    crc = next(s for s in mine["services"] if s["service_key"] == world.service.service_key)
    assert [sl["seats"] for sl in crc["slots"]] == [4, 6] and crc["slots"][0]["held_appointments"] == 1 and crc["slots"][0]["reserved"] == 2 and crc["utilisation"] == 0.2
    assert get(api_client, h, f"/analytics/schedule?day={day.isoformat()}&district=D05")["totals"]["clinics"] == 1
    assert get(api_client, h, f"/analytics/schedule?day={day.isoformat()}&state=MP")["clinics"][0]["name"] == world.clinic.name
    assert get(api_client, h, "/analytics/schedule")["totals"]["slots"] == 0 and get(api_client, h, "/analytics/schedule?day=not-a-date", 422)["code"] == "VALIDATION_ERROR"
    assert get(api_client, auth_headers("clinical_reviewer"), "/analytics/schedule", 403)["code"] == "FORBIDDEN"  # no capacity.view


# ------------------------------------------------------------------------------------------------ funnel period comparison
def test_funnel_compare_previous_rebuilds_the_stage_counts(api_client, auth_headers, world, sessions, db_session):
    def case(district, status, *steps):
        c = world.case(status)
        db_session.get(Resident, c.resident_id).district_code = district
        timeline(db_session, c, *steps)
        return c

    case("D01", "planned", (None, "identified", 40), ("identified", "planned", 35))
    case("D02", "identified", (None, "identified", 5))
    case("D01", "completed", (None, "identified", 50), ("identified", "planned", 48), ("planned", "outreach_pending", 46), ("outreach_pending", "completed", 45))
    case("D01", "planned", (None, "identified", 10), ("identified", "planned", 8))
    h = auth_headers("observer")
    plain = get(api_client, h, "/analytics/funnel")
    assert plain["comparison"] is None and all(st.get("previous") is None for st in plain["stages"])
    data = get(api_client, h, "/analytics/funnel?compare=previous&days=30")
    cmp, st = data["comparison"], {x["key"]: x for x in data["stages"]}
    assert cmp["available"] is True and cmp["window_days"] == 30 and cmp["definition"] and set(cmp["not_available"]) == {"residents", "due"}
    assert (st["case_opened"]["count"], st["case_opened"]["previous"]) == (4, {"count": 2, "cases": 2, "delta": 2, "delta_pct": 100.0})
    assert (st["planned"]["count"], st["planned"]["previous"]) == (3, {"count": 2, "cases": 2, "delta": 1, "delta_pct": 50.0})
    assert st["in_outreach"]["previous"] == {"count": 1, "cases": 1, "delta": 0, "delta_pct": 0.0}
    assert st["closed"]["previous"] == {"count": 1, "cases": 1, "delta": 0, "delta_pct": 0.0}  # completed 45 days ago
    assert st["residents"]["previous"] is None and st["due"]["previous"] is None  # not rebuildable: reported, not invented
    assert st["evaluated"]["previous"]["delta"] == 0
    d1 = {x["key"]: x for x in get(api_client, h, "/analytics/funnel?compare=previous&days=30&district=D01")["stages"]}
    assert (d1["case_opened"]["count"], d1["case_opened"]["previous"]["count"]) == (3, 2) and d1["closed"]["previous"]["count"] == 1
    assert get(api_client, h, "/analytics/funnel?compare=previous&days=10", 422)["code"] == "VALIDATION_ERROR"
    snapshot.refresh(sessions, reason="test")
    db_session.execute(text("UPDATE analytics_snapshots SET payload = payload - 'funnel_previous'"))  # a snapshot from before this feature
    old = get(api_client, h, "/analytics/funnel?compare=previous&days=7")["comparison"]
    assert old["available"] is False and "refresh" in old["reason"]
