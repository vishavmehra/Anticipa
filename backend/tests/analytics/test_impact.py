"""The impact report endpoint and the serving side (anticipa.analytics.impact): newest file, stored copy fallback, RBAC, ETag.
The computation lives in the evaluation package (tests/evaluation/test_impact_baselines.py). DB `anticipa_test`, rolled back."""
# ruff: noqa: E501

from __future__ import annotations

import json

from anticipa.analytics import impact

API = "/api/v1"

# ------------------------------------------------------------------------------------------------ report endpoint
REPORT = {"schema_version": "anticipa.impact.v1", "headlines": ["one", "two"], "rules_baseline": {"scope": {"residents": 3}}, "planner_baseline": {"difference": {}}}


def test_endpoint_serves_the_newest_report_to_analytics_viewers_only(api_client, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setattr(impact, "REPORTS_DIR", tmp_path)
    impact._CACHE.clear()
    url = f"{API}/analytics/impact"
    assert api_client.get(url, headers=auth_headers("observer")).status_code == 404  # nothing generated yet, a clear error rather than a crash
    (tmp_path / "impact-baselines-20261112.json").write_text(json.dumps({**REPORT, "headlines": ["old"]}), encoding="utf-8")
    (tmp_path / "impact-baselines-20261113.json").write_text(json.dumps(REPORT), encoding="utf-8")
    for role, status in {"system_admin": 200, "population_admin": 200, "observer": 200, "clinical_reviewer": 200, "care_coordinator": 403}.items():
        r = api_client.get(url, headers=auth_headers(role))
        assert r.status_code == status, (role, r.text)
        if status == 200:
            assert r.json()["data"] == REPORT and r.json()["meta"]["request_id"]  # the newest file by name
    first = api_client.get(url, headers=auth_headers("observer"))
    assert first.headers["cache-control"].startswith("private, max-age=")
    again = api_client.get(url, headers={**auth_headers("observer"), "If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert api_client.get(url).status_code == 401
    for method in ("post", "put", "patch", "delete"):
        assert api_client.request(method, url, headers=auth_headers("population_admin"), json={}).status_code == 405


def test_endpoint_reads_the_file_again_when_it_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(impact, "REPORTS_DIR", tmp_path)
    impact._CACHE.clear()
    path = tmp_path / "impact-baselines-20261113.json"
    path.write_text(json.dumps(REPORT), encoding="utf-8")
    assert impact.load_report()["headlines"] == ["one", "two"]
    path.write_text(json.dumps({**REPORT, "headlines": ["three"]}), encoding="utf-8")
    assert impact.load_report()["headlines"] == ["three"]
