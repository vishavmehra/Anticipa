"""Live processing monitor (integration, DB `anticipa_test`, rolled back; manifests in a temp dir): the pipeline reads
`resident_target` from the manifest (never assumes 1M), generation / loading progress, throughput and ETA, jobs,
AI spend against the configured caps, document counts, and the 10 s cache."""
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from anticipa.ai.models import ModelInvocation
from anticipa.analytics import service
from anticipa.datafabric import synthea
from anticipa.datafabric.models import Dataset, Resident
from anticipa.jobs.models import ProcessingJob

API = "/api/v1"


def now() -> datetime:  # evaluated per call: a slow run must not age the fixtures
    return datetime.now(UTC)


def get(client, headers, path="/analytics/processing"):
    r = client.get(f"{API}{path}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def write_manifest(version: str, *, target=200_000, batch_size=500, updated_ago_s=60, mtime=None):
    d = synthea.SYNTHEA_DIR / version
    d.mkdir(parents=True)
    t0 = now() - timedelta(hours=1)

    def batch(i, status="completed", finished_min=None, **extra):
        out = {"index": i, "status": status, "residents": batch_size, "seed": 42 + i}
        if finished_min is not None:
            out["finished_at"] = (t0 + timedelta(minutes=finished_min)).isoformat()
        return {**out, **extra}

    batches = {  # four real batches 10 minutes apart, one recovered (no timing), one running, one failed
        "0": batch(0, finished_min=0), "1": batch(1, finished_min=10), "2": batch(2, finished_min=20), "3": batch(3, finished_min=30),
        "4": batch(4, finished_min=31, recovered=True), "5": batch(5, "running"), "6": batch(6, "failed"),
    }  # fmt: skip
    manifest = {
        "dataset_version": version, "resident_target": target, "batch_size": batch_size, "status": "incomplete", "batches": batches,
        "updated_at": (now() - timedelta(seconds=updated_ago_s)).isoformat(),
    }  # fmt: skip
    path = d / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if mtime:
        os.utime(path, (mtime, mtime))
    return manifest


def test_pipeline_progress_uses_the_manifest_target_not_one_million(api_client, auth_headers):
    version = "syn-20261108-s42"
    write_manifest(version, target=200_000)
    p = get(api_client, auth_headers("system_admin"))["pipeline"]
    assert p["available"] and p["dataset_version"] == version and p["source"] == "synthetic" and p["definition"]
    assert p["resident_target"] == 200_000 and p["batch_size"] == 500
    g = p["generation"]
    assert g["batches_planned"] == 400 and g["batches_completed"] == 5 and g["batches_running"] == 1 and g["batches_failed"] == 1
    assert g["residents_generated"] == 2500 and g["percent"] == round(2500 / 200_000, 4)
    # inter-arrival of the 4 timed batches: 1500 residents over 30 min = 3000/h; recovered batch excluded from the rate
    assert g["throughput_residents_per_hour"] == 3000 and g["eta_hours"] == round((200_000 - 2500) / 3000, 1)
    assert p["state"] == "running" and p["idle_seconds"] <= 120
    assert p["ingestion"]["dataset_loaded"] is False and p["ingestion"]["residents_loaded"] == 0 and p["eta_hours"] is None


def test_ingestion_progress_profiles_and_eta(api_client, auth_headers, db_session):
    version = "syn-20261108-s43"
    write_manifest(version, target=10_000)
    ingest = {
        "0": {"residents_staged": 500, "loaded_at": (now() - timedelta(hours=3)).isoformat()},  # full (no profile key), older than 1 h
        "1": {"residents_staged": 500, "profile": "rules", "loaded_at": (now() - timedelta(minutes=30)).isoformat()},
        "2": {"residents_staged": 500, "profile": "rules", "loaded_at": (now() - timedelta(minutes=10)).isoformat()},
    }
    ds = Dataset(dataset_version=version, generator="test", status="loading", manifest={"ingest": {"batches": ingest}})
    db_session.add(ds)
    db_session.flush()
    for _ in range(3):
        db_session.add(Resident(dataset_id=ds.id, source_system="test", source_patient_id=uuid.uuid4().hex))
    db_session.flush()
    p = get(api_client, auth_headers("system_admin"), f"/analytics/processing?dataset_version={version}")["pipeline"]
    ing = p["ingestion"]
    assert ing["dataset_loaded"] and ing["batches_loaded"] == 3 and ing["residents_loaded"] == 3 and ing["dataset_status"] == "loading"
    assert ing["profiles"] == {"full": 500, "rules": 1000} and ing["profiles_basis"]
    assert ing["throughput_residents_per_hour"] == 1000  # 500 + 500 residents loaded inside the last hour
    assert ing["eta_hours"] == round((10_000 - 3) / 1000, 1) and ing["percent"] == round(3 / 10_000, 4)
    assert p["eta_hours"] == ing["eta_hours"]  # the slower of loading (10.0 h) and generation (2.5 h)


def test_latest_manifest_wins_and_missing_manifest_is_reported(api_client, auth_headers):
    h = auth_headers("system_admin")
    assert get(api_client, h)["pipeline"] == {
        "source": "synthetic", "definition": get(api_client, h)["pipeline"]["definition"], "dataset_version": None, "available": False,
        "message": "No generation manifest found for this dataset version.",
    }
    write_manifest("syn-20261101-s1", target=1000, mtime=now().timestamp() - 7200)
    write_manifest("syn-20261102-s2", target=2000, mtime=now().timestamp() - 60)
    assert get(api_client, h)["pipeline"]["dataset_version"] == "syn-20261102-s2"
    assert get(api_client, h, "/analytics/processing?dataset_version=syn-20261101-s1")["pipeline"]["resident_target"] == 1000
    missing = get(api_client, h, "/analytics/processing?dataset_version=syn-20261199-s9")["pipeline"]
    assert missing["available"] is False and missing["dataset_version"] == "syn-20261199-s9"
    bad = api_client.get(f"{API}/analytics/processing?dataset_version=../../etc", headers=h)
    assert bad.status_code == 422


def test_idle_pipeline_and_complete_pipeline_state(api_client, auth_headers):
    write_manifest("syn-20261108-s44", target=1000, updated_ago_s=7200)
    assert get(api_client, auth_headers("system_admin"))["pipeline"]["state"] == "idle"


def test_jobs_section(api_client, auth_headers, db_session):
    db_session.add_all([
        ProcessingJob(job_type="test.running", status="running", total_items=200, processed_items=50, succeeded_items=50,
                      heartbeat_at=now() - timedelta(seconds=5)),
        ProcessingJob(job_type="test.failed", status="failed", error_summary="x" * 500),
        ProcessingJob(job_type="test.done", status="completed", total_items=10, processed_items=10),
    ])  # fmt: skip
    db_session.flush()
    jobs = get(api_client, auth_headers("system_admin"))["jobs"]
    assert jobs["source"] == "live" and jobs["definition"]
    assert jobs["by_status"]["running"] >= 1 and jobs["by_status"]["completed"] >= 1 and jobs["failed_last_24h"] >= 1
    running = next(j for j in jobs["active"] if j["job_type"] == "test.running")
    assert running["percent_complete"] == 0.25 and running["heartbeat_age_seconds"] < 60
    failed = next(j for j in jobs["recent"] if j["job_type"] == "test.failed")
    assert len(failed["error_summary"]) == 200 and "checkpoint" not in failed


def test_ai_spend_against_budgets(api_client, auth_headers, db_session):
    job_a, job_b = uuid.uuid4(), uuid.uuid4()

    def call(provider, model, cost, job=None, status="succeeded", cache_hit=False):
        return ModelInvocation(task="evidence.extract", provider=provider, model_id=model, cost_usd=Decimal(cost), job_id=job, status=status,
                               input_tokens=100, output_tokens=10, cache_hit=cache_hit)  # fmt: skip

    db_session.add_all([
        call("anthropic", "haiku-x", "1.25", job_a), call("anthropic", "haiku-x", "0.75", job_a, cache_hit=True),
        call("anthropic", "sonnet-x", "0.50", job_b, status="failed"), call("nebius", "embed-x", "0.40"),
    ])  # fmt: skip
    db_session.flush()
    spend = get(api_client, auth_headers("system_admin"))["ai_spend"]
    assert spend["source"] == "live" and "ANTICIPA_AI_RUN_BUDGET_USD" in spend["definition"]
    assert spend["total_usd"] == 2.9 and spend["by_provider"] == {"anthropic": 2.5, "nebius": 0.4} and spend["invocations"] == 4
    a = spend["anthropic"]
    assert a["spend_usd"] == 2.5 and a["run_budget_usd"] == 40.0
    assert a["largest_run_spend_usd"] == 2.0 and a["largest_run_share_of_budget"] == 0.05
    assert a["spent_today_usd"] == 2.9 and a["daily_budget_usd"] == 5.0 and a["daily_share_of_budget"] == 0.58
    n = spend["nebius"]
    assert n["enabled"] is False and n["spend_usd"] == 0.4 and n["credit_usd"] == 50.0 and n["remaining_usd"] == 49.6
    assert n["share_of_credit_used"] == 0.008
    haiku = next(m for m in spend["models"] if m["model_id"] == "haiku-x")
    assert (haiku["calls"], haiku["cost_usd"], haiku["cache_hits"], haiku["input_tokens"]) == (2, 2.0, 1, 200)
    assert next(m for m in spend["models"] if m["model_id"] == "sonnet-x")["failed_calls"] == 1


def test_documents_section(api_client, auth_headers, seed, db_session):
    r = seed.resident()
    seed.document(r)
    seed.fact(r)
    seed.fact(r, validation="requires_review")
    docs = get(api_client, auth_headers("system_admin"))["documents"]
    assert docs["source"] == "synthetic" and docs["documents_total"] == sum(docs["documents_by_status"].values()) >= 1
    assert docs["evidence_facts_by_validation_status"]["accepted"] >= 1 and docs["evidence_facts_by_validation_status"]["requires_review"] >= 1
    assert docs["evidence_facts_total"] == sum(docs["evidence_facts_by_validation_status"].values())


def test_processing_is_cached_for_the_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "PROCESSING_TTL_S", 60.0)
    monkeypatch.setattr(service, "_processing", lambda session, version, now: calls.append(version) or {"n": len(calls)})
    assert service.processing(None) == {"n": 1} and service.processing(None) == {"n": 1}  # served from cache
    assert service.processing(None, dataset_version="syn-20261108-s42") == {"n": 2}  # distinct key
    service.clear_cache()
    assert service.processing(None) == {"n": 3} and len(calls) == 3
    monkeypatch.setattr(service, "PROCESSING_TTL_S", 0.0)
    assert service.processing(None) == {"n": 4} and service.processing(None) == {"n": 5}  # TTL 0 bypasses the cache


def test_command_center_and_funnel_cache_keys_are_distinct(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "COMMAND_CENTER_TTL_S", 60.0)
    monkeypatch.setattr(service, "_command_center", lambda session, sessions, limited, internal, days, compare, scope, now: calls.append((limited, internal, days)) or {"k": len(calls)})
    service.command_center(None, limited=False, trend_days=30)
    service.command_center(None, limited=False, trend_days=30)
    service.command_center(None, limited=True, trend_days=30)
    service.command_center(None, limited=False, trend_days=7)
    service.command_center(None, limited=False, internal=True, trend_days=30)  # internal viewers never share the product cache
    assert calls == [(False, False, 30), (True, False, 30), (False, False, 7), (False, True, 30)]
