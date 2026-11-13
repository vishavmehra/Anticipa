"""India map analytics (integration, DB `anticipa_test`, rolled back): the pilot district config (ADR-068), the geography
endpoint at country, state and district level with real counts, states without coverage, programme breakdowns and the
programmes endpoint. Resident rows are never changed; the mapping is applied when reading."""
# ruff: noqa: E501

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from anticipa.analytics import geography, service
from anticipa.guidelines.models import EligibilityEvaluation

API = "/api/v1"
FULL_PATH = ["identified", "approved_for_planning", "planned", "outreach_pending", "outreach_completed",
             "appointment_scheduled", "awaiting_completion_evidence", "completed"]  # fmt: skip
ACTIVITY = "colorectal_cancer_screening"


def get(client, headers, path, status=200):
    r = client.get(f"{API}{path}", headers=headers)
    assert r.status_code == status, r.text
    return r.json()["data"] if status == 200 else r.json()["error"]


# ------------------------------------------------------------------------------------------------ the config file
def test_district_map_config_is_complete_and_invents_nothing():
    m = geography.district_map()
    districts, states = m["districts"], m["states"]
    assert [d["code"] for d in districts] == [f"D{i:02d}" for i in range(1, 11)]
    assert len(states) == 36 and len({s["code"] for s in states}) == 36 and len({s["iso_3166_2"] for s in states}) == 36
    assert all(s["iso_3166_2"].startswith("IN-") and s["code"].isdigit() and s["type"] in ("state", "union_territory") for s in states)
    by_code = {s["code"]: s for s in states}
    assert {d["state"] for d in districts} == {"Madhya Pradesh", "Maharashtra", "Uttar Pradesh", "Karnataka"}
    assert [d["district"] for d in districts if d["state"] == "Madhya Pradesh"] == ["Bhopal", "Indore", "Jabalpur", "Gwalior"]  # weighted to MP
    assert [d["district"] for d in districts][4:] == ["Pune", "Nagpur", "Lucknow", "Varanasi", "Bengaluru Urban", "Mysuru"]
    for d in districts:
        assert by_code[d["state_code"]]["name"] == d["state"] and by_code[d["state_code"]]["iso_3166_2"] == d["iso_3166_2"]
        assert d["lgd_district_code"] is None  # LGD district codes are not verified, so never guessed
        assert 8 < d["lat"] < 37 and 68 < d["lon"] < 98
    assert {by_code[c]["name"] for c in ("23", "27", "09", "29")} == {"Madhya Pradesh", "Maharashtra", "Uttar Pradesh", "Karnataka"}


# ------------------------------------------------------------------------------------------------ country, state, district
def seed_geo(make_case):
    make_case("planned", district="D01", priority="high", path=["identified", "approved_for_planning", "planned"])
    make_case("completed", district="D01", path=FULL_PATH)
    make_case("identified", district="D02")
    make_case("outreach_pending", district="D05", path=FULL_PATH[:4])
    make_case("cancelled", district="D09", path=["identified", "approved_for_planning", "planned", "cancelled"])
    make_case("identified", district="D99")  # not in the pilot map


def areas(data):
    return {a["code"]: a for a in data["areas"]}


def test_country_level_lists_every_state_with_real_counts(api_client, auth_headers, make_case):
    seed_geo(make_case)
    data = get(api_client, auth_headers("observer"), "/analytics/geography")
    assert data["level"] == "country" and data["area"]["name"] == "India" and data["area"]["population"] == 6
    st = areas(data)
    assert len(st) == 36 and data["pilot"] == {"districts": 10, "states": 4, "states_listed": 36, "definition": data["pilot"]["definition"]}
    mp = st["23"]
    assert (mp["name"], mp["iso_code"], mp["coverage_status"], mp["district_codes"]) == ("Madhya Pradesh", "IN-MP", "pilot", ["D01", "D02", "D03", "D04"])
    assert (mp["population"], mp["open_care_gaps"], mp["completed"], mp["cases_total"], mp["in_outreach"], mp["due_residents"]) == (3, 2, 1, 3, 0, 0)
    assert mp["gaps_per_100"] == round(200 / 3, 2) and mp["closure_rate"] == round(1 / 3, 4) and mp["coverage"] == round(2 / 3, 4)
    assert mp["residents_with_open_gaps"] == 2
    prog = mp["programmes"][0]
    assert prog["care_activity"] == ACTIVITY and prog["label"] == "Colorectal cancer screening" and prog["cases_total"] == 3 and prog["completed"] == 1
    mh, ka = st["27"], st["29"]
    assert (mh["population"], mh["open_care_gaps"], mh["in_outreach"], mh["coverage"]) == (1, 1, 1, 1.0)
    assert (ka["population"], ka["open_care_gaps"], ka["completed"], ka["coverage"], ka["closure_rate"]) == (1, 0, 0, 1.0, 0.0)  # cancelled after being planned
    assert data["unmapped"]["district_codes"] == ["D99"] and data["unmapped"]["population"] == 1  # reconciles with the country total
    assert data["area"]["population"] == sum(a["population"] for a in data["areas"]) + data["unmapped"]["population"]


def test_states_without_pilot_districts_show_no_coverage(api_client, auth_headers, make_case):
    seed_geo(make_case)
    st = areas(get(api_client, auth_headers("observer"), "/analytics/geography"))
    up, kerala = st["09"], st["32"]  # Uttar Pradesh has pilot districts (Lucknow, Varanasi) but no residents here; Kerala has none at all
    assert up["coverage_status"] == "pilot" and up["population"] == 0 and up["district_codes"] == ["D07", "D08"] and up["closure_rate"] is None
    assert kerala["coverage_status"] == "no_coverage" and kerala["population"] == 0 and kerala["district_codes"] == []
    assert kerala["gaps_per_100"] is None and kerala["coverage"] is None and kerala["programmes"] == [] and "No pilot district" in kerala["message"]
    assert sum(1 for a in st.values() if a["coverage_status"] == "no_coverage") == 32


def test_state_level_drills_down_to_districts_and_accepts_three_code_forms(api_client, auth_headers, make_case):
    seed_geo(make_case)
    h = auth_headers("observer")
    results = [get(api_client, h, f"/analytics/geography?level=state&code={c}") for c in ("23", "MP", "in-mp")]
    assert all(r["area"]["name"] == "Madhya Pradesh" and r["area"]["population"] == 3 for r in results)
    d = areas(results[0])
    assert list(d) == ["D01", "D02", "D03", "D04"]
    assert (d["D01"]["name"], d["D01"]["population"], d["D01"]["open_care_gaps"], d["D01"]["completed"]) == ("Bhopal", 2, 1, 1)
    assert (d["D03"]["name"], d["D03"]["population"], d["D03"]["coverage_status"]) == ("Jabalpur", 0, "pilot")
    assert d["D01"]["lat"] and d["D01"]["state"] == "Madhya Pradesh" and d["D01"]["lgd_district_code"] is None
    empty = get(api_client, h, "/analytics/geography?level=state&code=32")
    assert empty["area"]["coverage_status"] == "no_coverage" and empty["areas"] == []


def test_district_level_has_the_programme_breakdown(api_client, auth_headers, make_case):
    seed_geo(make_case)
    data = get(api_client, auth_headers("observer"), "/analytics/geography?level=district&code=d01")
    a = data["area"]
    assert (a["level"], a["code"], a["name"], a["state"], a["iso_code"], a["population"], a["open_care_gaps"], a["completed"]) == ("district", "D01", "Bhopal", "Madhya Pradesh", "IN-MP", 2, 1, 1)
    assert data["areas"] == [] and a["programmes"][0]["care_activity"] == ACTIVITY and a["programmes"][0]["cases_total"] == 2


def test_due_residents_come_from_latest_evaluations_by_programme(api_client, auth_headers, seed, seed_module, db_session):
    version, _ = seed.approved_version(care_activity="geo_demo_activity")
    residents = [seed.resident(district="D01"), seed.resident(district="D01"), seed.resident(district="D05")]
    seed_module.run_evaluation(db_session, [version], date(2026, 11, 8), resident_ids=[r.id for r in residents])
    due = {e.resident_id for e in db_session.execute(select(EligibilityEvaluation)).scalars() if e.outcome in ("eligible", "requires_clinical_review")}
    expected = {"D01": len(due & {residents[0].id, residents[1].id}), "D05": len(due & {residents[2].id})}
    assert sum(expected.values()) == len(due) and due
    h = auth_headers("observer")
    d1 = get(api_client, h, "/analytics/geography?level=district&code=D01")["area"]
    assert d1["population"] == 2 and d1["due_residents"] == expected["D01"]
    assert [(p["care_activity"], p["due_residents"]) for p in d1["programmes"]] == [("geo_demo_activity", expected["D01"])] and d1["programmes"][0]["cases_total"] == 0
    mp = areas(get(api_client, h, "/analytics/geography"))
    assert mp["27"]["due_residents"] == expected["D05"] and mp["23"]["due_residents"] == expected["D01"]


def test_definitions_are_returned_for_every_measure(api_client, auth_headers, make_case):
    seed_geo(make_case)
    data = get(api_client, auth_headers("observer"), "/analytics/geography")
    keys = set(geography.DEFINITIONS)
    assert keys == {"population", "due_residents", "open_care_gaps", "residents_with_open_gaps", "cases_total", "completed", "in_outreach", "gaps_per_100", "closure_rate", "coverage", "up_to_date_residents", "screening_coverage"}
    assert set(data["definitions"]) == keys
    for k, m in data["definitions"].items():
        assert m["label"] and m["definition"] and m["source"] in service.SOURCES, k
    assert set(data["areas"][0]) >= {"population", "programmes", "coverage_status", "name", "code", "gaps_per_100", "closure_rate", "coverage"}


def test_geography_validation_and_not_found(api_client, auth_headers, make_case):
    h = auth_headers("observer")
    assert get(api_client, h, "/analytics/geography?level=state", 422)["code"] == "VALIDATION_ERROR"  # a state needs a code
    assert get(api_client, h, "/analytics/geography?level=district", 422)["code"] == "VALIDATION_ERROR"
    assert get(api_client, h, "/analytics/geography?level=planet", 422)["code"] == "VALIDATION_ERROR"
    assert get(api_client, h, "/analytics/geography?level=state&code=ZZ", 404)["code"] == "NOT_FOUND"
    assert get(api_client, h, "/analytics/geography?level=district&code=D77", 404)["code"] == "NOT_FOUND"


def test_empty_database_still_lists_all_states_with_zero_population(api_client, auth_headers, db_session):
    data = get(api_client, auth_headers("observer"), "/analytics/geography")
    assert len(data["areas"]) == 36 and data["level"] == "country"
    assert all(a["closure_rate"] is None and a["coverage"] is None and a["gaps_per_100"] is None for a in data["areas"] if a["population"] == 0)
    assert sum(a["population"] for a in data["areas"]) == data["area"]["population"] - (data["unmapped"] or {"population": 0})["population"]


# ------------------------------------------------------------------------------------------------ programmes
def test_programmes_endpoint_lists_care_activities(api_client, auth_headers, make_case):
    seed_geo(make_case)
    make_case("completed", district="D02", path=FULL_PATH, activity="breast_cancer_screening")
    data = get(api_client, auth_headers("observer"), "/analytics/programmes")
    rows = {p["care_activity"]: p for p in data["programmes"]}
    assert set(rows) == {ACTIVITY, "breast_cancer_screening"} and data["population"] == 7
    crc = rows[ACTIVITY]
    assert (crc["cases_total"], crc["open_care_gaps"], crc["completed"], crc["in_outreach"]) == (6, 4, 1, 1)
    assert crc["closure_rate"] == round(1 / 6, 4) and crc["label"] == "Colorectal cancer screening"
    assert rows["breast_cancer_screening"]["closure_rate"] == 1.0 and data["programmes"][0]["care_activity"] == ACTIVITY  # largest first
    assert "programme" in data["definitions"] and all(m["definition"] for m in data["definitions"].values())


# ------------------------------------------------------------------------------------------------ programme display names
def test_programme_labels_keep_acronyms_in_capitals(api_client, auth_headers, make_case):
    from anticipa.analytics import places

    expected = {
        "brca_risk_assessment": "BRCA risk assessment", "brca_genetic_counseling_referral": "BRCA genetic counseling referral",
        "india_cervical_cancer_screening_via": "India cervical cancer screening VIA", "india_breast_cancer_screening_cbe": "India breast cancer screening CBE",
        "colorectal_family_history": "Colorectal family history", "breast_cancer_screening": "Breast cancer screening", "": "",
    }  # fmt: skip
    assert {k: places.programme_label(k) for k in expected} == expected
    assert places.programme_label("hpv_vaccination") == "HPV vaccination" and places.programme_label("_x__y_") == "X y"
    make_case("planned", activity="brca_risk_assessment")
    make_case("planned", activity="brca_genetic_counseling_referral")
    h = auth_headers("observer")
    labels = {p["care_activity"]: p["label"] for p in get(api_client, h, "/analytics/programmes")["programmes"]}
    assert labels == {"brca_risk_assessment": "BRCA risk assessment", "brca_genetic_counseling_referral": "BRCA genetic counseling referral"}
    demand = get(api_client, h, "/analytics/demand-summary")
    assert {p["label"] for p in demand["by_programme"]} == set() or all("Brca" not in p["label"] for p in demand["by_programme"])  # planned cases are not waiting for a slot
    district = get(api_client, h, "/analytics/geography?level=district&code=D01")["area"]["programmes"]
    assert all("Brca" not in p["label"] for p in district)
