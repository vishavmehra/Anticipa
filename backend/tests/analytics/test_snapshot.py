"""Precomputed analytics (integration, DB `anticipa_test`, rolled back): one stored snapshot serves every endpoint, equals
the live computation, falls back to live when absent, refreshes from the CLI, the worker hook and a data change check,
and the analytics GETs carry Cache-Control and ETag / 304."""
# ruff: noqa: E501

from __future__ import annotations

import json
import threading

from sqlalchemy import func, select, text

from anticipa.analytics import snapshot
from anticipa.analytics.models import AnalyticsSnapshot
from anticipa.jobs import worker

API = "/api/v1"
FULL_PATH = ["identified", "approved_for_planning", "planned", "outreach_pending", "outreach_completed",
             "appointment_scheduled", "awaiting_completion_evidence", "completed"]  # fmt: skip
PRODUCT_CACHE = "private, max-age=30, stale-while-revalidate=120"


def kpi(client, headers, key):
    r = client.get(f"{API}/analytics/command-center", headers=headers)
    assert r.status_code == 200, r.text
    return {k["key"]: k["value"] for k in r.json()["data"]["kpis"]}[key]


def rows(db_session):
    return db_session.scalar(select(func.count()).select_from(AnalyticsSnapshot))


# ------------------------------------------------------------------------------------------------ refresh and read
def test_refresh_stores_one_row_and_the_endpoints_serve_it(api_client, auth_headers, make_case, sessions, db_session):
    h = auth_headers("observer")
    make_case("planned")
    make_case("identified")
    out = snapshot.refresh(sessions, reason="test")
    assert out["refreshed"] is True and out["reason"] == "test" and out["case_rows"] >= 1 and rows(db_session) == 1
    data = api_client.get(f"{API}/analytics/command-center", headers=h).json()["data"]
    assert data["snapshot"]["source"] == "snapshot" and data["snapshot"]["age_seconds"] >= 0 and data["snapshot"]["definition"]
    assert kpi(api_client, h, "care_gaps_total") == 2
    make_case("planned")  # changed after the snapshot: the endpoints keep serving the precomputed figures
    assert kpi(api_client, h, "care_gaps_total") == 2
    funnel = api_client.get(f"{API}/analytics/funnel", headers=h).json()["data"]
    assert {s["key"]: s["cases"] for s in funnel["stages"]}["case_opened"] == 2 and funnel["snapshot"]["source"] == "snapshot"
    snapshot.refresh(sessions, reason="test again")
    assert rows(db_session) == 1 and kpi(api_client, h, "care_gaps_total") == 3  # an upsert, not a second row


def test_snapshot_equals_the_live_computation(make_case, sessions, db_session):
    make_case("planned", district="D01", path=["identified", "approved_for_planning", "planned"])
    make_case("completed", district="D05", path=FULL_PATH)
    snapshot.refresh(sessions, reason="test")
    stored = db_session.scalar(select(AnalyticsSnapshot.payload).where(AnalyticsSnapshot.key == snapshot.CORE_KEY))
    live = snapshot.compute_core(db_session)
    ignore = {"computed_at", "duration_ms", "fingerprint", "stocks", "funnel_previous"}  # these carry timestamps taken at compute time
    assert {k: v for k, v in stored.items() if k not in ignore} == json.loads(json.dumps({k: v for k, v in live.items() if k not in ignore}))
    assert stored["version"] == snapshot.CORE_VERSION and sum(stored["residents"].values()) == 2
    assert set(stored["stocks"]) == {str(d) for d in snapshot.COMPARE_DAYS} == set(stored["funnel_previous"]) and set(live["stocks"]) == set(stored["stocks"])


def test_live_fallback_without_a_snapshot_or_before_the_migration(api_client, auth_headers, make_case, db_session):
    make_case("planned")
    core, meta = snapshot.load_core(db_session)
    assert meta["source"] == "live" and sum(core["residents"].values()) == 1
    assert api_client.get(f"{API}/analytics/funnel", headers=auth_headers("observer")).json()["data"]["snapshot"]["source"] == "live"
    db_session.execute(text("DROP TABLE analytics_snapshots"))  # migration 0008 not applied yet (rolled back with the test)
    assert snapshot._read(db_session) is None
    core, meta = snapshot.load_core(db_session)
    assert meta["source"] == "live" and sum(core["residents"].values()) == 1
    assert db_session.scalar(text("SELECT 1")) == 1  # the failed read did not poison the transaction


# ------------------------------------------------------------------------------------------------ freshness
def test_a_changed_fingerprint_starts_one_background_refresh(monkeypatch, db_session, sessions):
    snapshot.refresh(sessions, reason="test")
    calls = []
    monkeypatch.setattr(snapshot, "AUTO_REFRESH", True)
    monkeypatch.setattr(snapshot, "AUTO_REFRESH_GAP_S", 0.0)
    monkeypatch.setattr(snapshot, "_background_refresh", lambda s, reason: calls.append(reason))
    monkeypatch.setattr(snapshot, "fingerprint", lambda session: 123456)  # differs from the stored value
    _, meta = snapshot.load_core(db_session, sessions)
    assert meta["source"] == "snapshot" and calls == ["auto: data changed"]
    stored = db_session.scalar(select(AnalyticsSnapshot.payload["fingerprint"].astext))
    monkeypatch.setattr(snapshot, "fingerprint", lambda session: int(stored))  # unchanged data: no refresh
    snapshot.load_core(db_session, sessions)
    monkeypatch.setattr(snapshot, "fingerprint", lambda session: 123456)
    snapshot.load_core(db_session)  # no sessionmaker given (not from the API): never refreshes
    monkeypatch.setattr(snapshot, "AUTO_REFRESH", False)
    snapshot.load_core(db_session, sessions)
    monkeypatch.setattr(snapshot, "AUTO_REFRESH", True)
    monkeypatch.setattr(snapshot, "AUTO_REFRESH_GAP_S", 3600.0)  # a fresh snapshot is not even checked
    snapshot.load_core(db_session, sessions)
    assert calls == ["auto: data changed"]


def test_no_snapshot_starts_a_background_refresh_and_answers_live(monkeypatch, db_session, make_case, sessions):
    make_case("planned")
    calls = []
    monkeypatch.setattr(snapshot, "AUTO_REFRESH", True)
    monkeypatch.setattr(snapshot, "_background_refresh", lambda s, reason: calls.append(reason))
    core, meta = snapshot.load_core(db_session, sessions)
    assert calls == ["auto: no snapshot"] and meta["source"] == "live" and sum(core["residents"].values()) == 1


def test_background_refresh_runs_in_a_thread_once_at_a_time(monkeypatch, sessions):
    started, release, done = threading.Event(), threading.Event(), []
    monkeypatch.setattr(snapshot, "AUTO_REFRESH_GAP_S", 0.0)
    monkeypatch.setattr(snapshot, "_LAST_AUTO", 0.0)

    def slow_refresh(s, reason):
        started.set()
        release.wait(5)
        done.append(reason)

    monkeypatch.setattr(snapshot, "refresh", slow_refresh)
    snapshot._background_refresh(sessions, "first")
    assert started.wait(5)
    snapshot._background_refresh(sessions, "second")  # one refresh at a time per process
    release.set()
    for _ in range(100):
        if done and snapshot._REFRESHING.acquire(blocking=False):
            snapshot._REFRESHING.release()
            break
        threading.Event().wait(0.05)
    assert done == ["first"]


def test_worker_hook_refreshes_after_analytics_relevant_jobs_only(monkeypatch, make_case, sessions, db_session):
    make_case("planned")
    snapshot.after_job(sessions, "system.noop")
    snapshot.after_job(sessions, "evaluation.run")
    assert rows(db_session) == 0  # not analytics relevant
    for job_type in ("planning.run", "engagement.simulate", "closure.sweep", "guidelines.evaluate", "caregaps.generate"):
        assert job_type.startswith(snapshot.REFRESH_JOB_PREFIXES)
    snapshot.after_job(sessions, "planning.run")
    first = db_session.scalar(select(AnalyticsSnapshot.computed_at))
    assert rows(db_session) == 1 and first is not None
    snapshot.after_job(sessions, "caregaps.generate")  # debounced: the snapshot is younger than JOB_REFRESH_GAP_S
    assert db_session.scalar(select(AnalyticsSnapshot.computed_at)) == first
    monkeypatch.setattr(snapshot, "JOB_REFRESH_GAP_S", -1.0)
    snapshot.after_job(sessions, "caregaps.generate")
    assert db_session.scalar(select(AnalyticsSnapshot.computed_at)) > first


def test_the_hook_never_fails_a_job(monkeypatch, sessions):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(snapshot, "refresh", boom)
    snapshot.after_job(sessions, "planning.run")  # swallowed and logged
    monkeypatch.setattr(snapshot, "after_job", boom)
    worker._refresh_analytics(sessions, "planning.run")  # the worker wrapper swallows it too


def test_worker_wrapper_refreshes_the_snapshot(make_case, sessions, db_session):
    make_case("planned")
    worker._refresh_analytics(sessions, "planning.run")
    assert rows(db_session) == 1


def test_cli_refresh_and_status(monkeypatch, capsys, make_case, sessions):
    import anticipa.db

    monkeypatch.setattr(anticipa.db, "get_sessionmaker", lambda: sessions)
    assert snapshot.main(["status"]) == 1 and json.loads(capsys.readouterr().out) == {"snapshot": None}
    make_case("planned")
    assert snapshot.main(["refresh"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["refreshed"] is True and out["reason"] == "cli" and out["residents"] == 1
    assert snapshot.main(["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["residents"] == 1 and status["age_seconds"] >= 0 and status["duration_ms"] >= 0


# ------------------------------------------------------------------------------------------------ HTTP caching
def test_analytics_gets_carry_cache_control_and_etag(api_client, auth_headers, make_case):
    make_case("planned")
    h = auth_headers("population_admin")
    for path in ("/analytics/command-center", "/analytics/funnel", "/analytics/geography", "/analytics/programmes"):
        r = api_client.get(f"{API}{path}", headers=h)
        assert r.status_code == 200 and r.headers["cache-control"] == PRODUCT_CACHE, path
        assert r.headers["etag"].startswith('W/"') and r.headers["vary"] == "Authorization", path
        assert r.json()["meta"]["request_id"] and "data" in r.json()
    resident_read = api_client.get(f"{API}/residents/{make_case('planned').resident_id}/journey", headers=h)
    assert resident_read.status_code == 200 and resident_read.headers["cache-control"] == "no-store"  # resident level reads are never cached (API default)


def test_if_none_match_returns_304_until_the_figures_change(api_client, auth_headers, make_case):
    h = auth_headers("observer")
    make_case("planned")
    first = api_client.get(f"{API}/analytics/command-center", headers=h)
    etag = first.headers["etag"]
    again = api_client.get(f"{API}/analytics/command-center", headers=h)
    assert again.headers["etag"] == etag  # recomputed (TTL 0) with new generated_at and request id, same figures, same ETag
    for header in (etag, f'"x", {etag}', "*", etag.removeprefix("W/")):
        r = api_client.get(f"{API}/analytics/command-center", headers={**h, "If-None-Match": header})
        assert r.status_code == 304 and r.content == b"" and r.headers["etag"] == etag and r.headers["cache-control"] == PRODUCT_CACHE, header
    other = api_client.get(f"{API}/analytics/command-center", headers={**h, "If-None-Match": '"stale"'})
    assert other.status_code == 200 and other.json()["data"]["kpis"]
    make_case("identified")  # the figures change, so the old ETag no longer matches
    changed = api_client.get(f"{API}/analytics/command-center", headers={**h, "If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag


def test_etag_is_per_representation_not_per_role(api_client, auth_headers, make_case):
    make_case("planned")
    full = api_client.get(f"{API}/analytics/command-center", headers=auth_headers("observer"))
    limited = api_client.get(f"{API}/analytics/command-center", headers=auth_headers("care_coordinator"))
    assert full.headers["etag"] != limited.headers["etag"]  # the limited view has different content
    r = api_client.get(f"{API}/analytics/command-center", headers={**auth_headers("care_coordinator"), "If-None-Match": full.headers["etag"]})
    assert r.status_code == 200


def test_processing_always_revalidates(api_client, auth_headers):
    h = auth_headers("system_admin")
    first = api_client.get(f"{API}/analytics/processing", headers=h)
    assert first.status_code == 200 and first.headers["cache-control"] == "private, no-cache"
    r = api_client.get(f"{API}/analytics/processing", headers={**h, "If-None-Match": first.headers["etag"]})
    assert r.status_code in (200, 304)  # 304 whenever the jobs, spend and pipeline figures did not move in between


def test_a_stored_snapshot_means_no_heavy_aggregates_per_request(api_client, auth_headers, make_case, sessions, monkeypatch):
    """Not a benchmark (those are in the handoff): with a snapshot the endpoints never run the heavy aggregate queries."""
    for _ in range(5):
        make_case("planned")
    snapshot.refresh(sessions, reason="test")
    calls = []
    original = snapshot.compute_core
    monkeypatch.setattr(snapshot, "compute_core", lambda *a, **k: calls.append(1) or original(*a, **k))
    h = auth_headers("observer")
    for path in ("/analytics/command-center", "/analytics/funnel", "/analytics/geography", "/analytics/programmes"):
        assert api_client.get(f"{API}{path}", headers=h).status_code == 200
    assert calls == []
