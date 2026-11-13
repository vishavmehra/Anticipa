"""Ask Anticipa tools against real rows, and the HTTP contract (integration, DB `anticipa_test`, rolled back)."""
# ruff: noqa: E501

from __future__ import annotations

import json
import re
import uuid

import pytest
from ask_samples import Script
from sqlalchemy import select

from anticipa.ai.testing import make_test_router
from anticipa.ask import service, tools
from anticipa.core.models import AuditEvent

API = "/api/v1"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def dist():
    return "Z" + uuid.uuid4().hex[:5]  # a district no other test uses


@pytest.fixture
def seeded(make_case):
    d = dist()
    for _ in range(5):
        make_case("completed", district=d, language="en")
    for _ in range(6):
        make_case("planned", district=d, language="hi")
    for _ in range(2):  # fewer than MIN_CELL cases in this group
        make_case("planned", district=d, language="ar", programme="breast_cancer_screening")
    return d


def run(session, name, args, scope=None):
    return tools.run_tool(session, name, json.dumps(args), scope or {}, "c1")


def test_breakdown_counts_rates_and_suppresses_small_groups(db_session, seeded):
    r = run(db_session, "get_care_gap_breakdown", {"group_by": "language", "district": seeded})
    by = {x["group"]: x for x in r["rows"]}
    assert by["en"]["cases"] == 5 and by["en"]["completed"] == 5 and by["en"]["closure_rate"] == 1.0
    assert by["hi"]["cases"] == 6 and by["hi"]["completed"] == 0 and by["hi"]["closure_rate"] == 0.0 and by["hi"]["open"] == 6
    assert "ar" not in by and any("suppressed" in n for n in r["notes"])  # 2 cases: below the cell minimum
    assert r["definition"] and r["source"] == "simulated" and r["units"]["closure_rate"] == "ratio"


def test_scope_set_by_the_ui_overrides_what_the_model_asks_for(db_session, seeded):
    other = dist()
    r = run(db_session, "get_care_gap_breakdown", {"group_by": "status", "district": other}, scope={"district": seeded})
    assert sum(x["cases"] for x in r["rows"]) == 13  # the UI scope (seeded district) won, not the model's empty district


def test_snapshot_backed_and_live_breakdowns_agree(db_session, seeded):
    by_status = run(db_session, "get_care_gap_breakdown", {"group_by": "status", "district": seeded})
    assert {x["group"]: x["cases"] for x in by_status["rows"]} == {"planned": 8, "completed": 5}
    by_age = run(db_session, "get_care_gap_breakdown", {"group_by": "age_band", "district": seeded})
    assert sum(x["cases"] for x in by_age["rows"]) == 13 and by_age["rows"][0]["group"] == "50 to 59"  # 1970-born residents


def test_every_tool_runs_and_returns_the_common_aggregate_shape(db_session, seeded):
    for name in tools.TOOLS:
        args = {"group_by": "district"} if name == "get_care_gap_breakdown" else {}
        out = tools.run_tool(db_session, name, json.dumps(args), {}, "c9")
        assert out["tool"] == name and out["call_id"] == "c9" and "error" not in out, (name, out)
        assert {"definition", "source", "values", "rows", "labels", "units", "notes"} <= set(out)
        assert len(out["rows"]) <= tools.MAX_ROWS
        assert not UUID_RE.search(json.dumps(out, default=str)), f"{name} returned an identifier"  # aggregates only


def test_status_open_is_every_non_terminal_status_and_unknown_filter_values_list_the_valid_ones(db_session, seeded):
    opened = run(db_session, "get_care_gap_breakdown", {"group_by": "priority", "status": "open", "district": seeded})
    assert [(x["group"], x["cases"]) for x in opened["rows"]] == [("routine", 8)]  # 13 cases, 5 completed
    for bad, key in (({"status": "not_a_status"}, "status"), ({"priority": "urgent"}, "priority"), ({"language": "fr"}, "language"), ({"programme": "x_screening"}, "programme"), ({"district": "D99"}, "district")):
        r = run(db_session, "get_care_gap_breakdown", {"group_by": "status", **bad})
        assert r["error"].startswith("unknown_filter_value") and key in r["valid_values"], r  # never an empty "zero cases" answer


def test_closure_trend_and_filters(db_session, seeded):
    r = run(db_session, "get_closure_trend", {"days": 7, "district": seeded})
    assert r["values"]["total_opened"] == 13 and r["values"]["total_closed"] == 5 and len(r["rows"]) == 7


def test_bad_input_comes_back_as_an_error_result_the_model_can_read(db_session):
    unknown = run(db_session, "get_resident", {"id": "x"})
    assert unknown["error"] == "unknown_tool" and "get_funnel" in unknown["valid_tools"]
    assert "invalid_arguments" in run(db_session, "get_care_gap_breakdown", {"group_by": "patient_name"})["error"]
    assert "invalid_arguments" in run(db_session, "get_closure_trend", {"days": 365})["error"]
    assert "invalid_arguments" in run(db_session, "get_funnel", {"sql": "select 1"})["error"]  # no free SQL, no extra arguments
    assert "invalid_arguments" in tools.run_tool(db_session, "get_funnel", "[1]", {}, "c1")["error"]
    assert "invalid_arguments" in tools.run_tool(db_session, "get_funnel", "not json", {}, "c1")["error"]


def test_no_tool_can_reach_resident_level_data():
    banned = {"resident", "document", "fact", "evidence", "patient", "case_id"}
    for name, tool in tools.TOOLS.items():
        fields = set(tool.args.model_fields)
        assert not any(b in f for f in fields for b in banned), (name, fields)


# ------------------------------------------------------------------------------------------------------ HTTP
@pytest.fixture
def scripted(monkeypatch):
    script = Script({"action": "call_tools", "calls": [{"tool": "get_funnel", "args_json": "{}"}]},
                    {"action": "answer", "answer": "Here is the care gap funnel by stage.", "table": {"source": "c1", "columns": ["label", "residents"], "title": "Funnel"}})  # fmt: skip
    monkeypatch.setattr(service, "get_router", lambda: make_test_router({"anthropic": script})[0])
    return script


def post(client, headers, **body):
    return client.post(f"{API}/ask", headers=headers, json={"question": "How does the care gap funnel look?", **body})


def test_observer_gets_a_grounded_answer_with_a_server_built_table_and_one_audit_event(api_client, auth_headers, db_session, scripted):
    r = post(api_client, auth_headers("observer"))
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["grounded"] and d["refused"] is False and d["table"]["columns"][0]["key"] == "label" and d["sources"][0]["tool"] == "get_funnel"
    assert [c["key"] for c in d["table"]["columns"]] == ["label", "residents"] and len(d["table"]["rows"]) >= 7
    ev = db_session.scalars(select(AuditEvent).where(AuditEvent.action == "ask.query").order_by(AuditEvent.occurred_at.desc())).first()
    assert ev.details["tools"] == ["get_funnel"] and ev.details["grounded"] is True and ev.details["question_hash"]
    assert "funnel look" not in json.dumps(ev.details)  # the question text itself is never audited


def test_permissions_and_validation(api_client, api_client_nodb, auth_headers, scripted):
    assert api_client_nodb.post(f"{API}/ask", json={"question": "hello there"}).status_code == 401
    assert post(api_client, auth_headers("care_coordinator")).status_code == 403  # analytics.view_limited only
    for role in ("observer", "population_admin", "system_admin"):
        assert post(api_client, auth_headers(role)).status_code == 200, role
    h = auth_headers("observer")
    assert api_client.post(f"{API}/ask", headers=h, json={"question": "hi"}).status_code == 422
    assert api_client.post(f"{API}/ask", headers=h, json={"question": "x" * 501}).status_code == 422
    turns = [{"role": "user", "content": "a"}] * 7
    assert api_client.post(f"{API}/ask", headers=h, json={"question": "valid question", "history": turns}).status_code == 422
    assert api_client.post(f"{API}/ask", headers=h, json={"question": "valid question", "filters": {"district": "D1; drop table"}}).status_code == 422


def test_rate_limit_is_a_429_with_retry_after(api_client, auth_headers, scripted, monkeypatch):
    from anticipa.ai.settings import AiSettings

    monkeypatch.setattr(service, "get_ai_settings", lambda: AiSettings(_env_file=None, anticipa_ask_rate_per_hour=2))
    h = auth_headers("observer")
    assert post(api_client, h, question="first question about funnel").status_code == 200
    assert post(api_client, h, question="second question about funnel").status_code == 200
    r = post(api_client, h, question="third question about funnel")
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED" and int(r.headers["Retry-After"]) > 0


def test_suggestions_are_built_from_live_data_without_a_model_call(api_client, auth_headers, seeded):
    r = api_client.get(f"{API}/ask/suggestions", headers=auth_headers("observer"))
    assert r.status_code == 200
    items = r.json()["data"]["suggestions"]
    assert 1 <= len(items) <= 4 and all(i["question"].endswith("?") for i in items)
    assert api_client.get(f"{API}/ask/suggestions", headers=auth_headers("care_coordinator")).status_code == 403
