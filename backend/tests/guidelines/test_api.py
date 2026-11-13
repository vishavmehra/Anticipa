"""Guideline / rule / evaluation HTTP API (05 §10-11) through the real app: envelopes, RBAC (authorisation
runs BEFORE body
validation, so a forbidden role gets 403 even with an invalid body), the workflow, evaluation jobs and what an
evaluation
returns. Integration: DB `anticipa_test`, rolled back."""

from __future__ import annotations

import re
import uuid
from datetime import date

import pytest

from anticipa.guidelines.models import GuidelineVersion

ON = date(2026, 11, 8)
TAGS = {"guidelines", "care-gaps"}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@pytest.fixture
def h(seed_module):
    return seed_module


def mutating_routes(app):
    """(method, concrete path) for every mutating operation of the guideline and care-gap APIs, from the
    OpenAPI document."""  # noqa: E501
    out = []
    for path, ops in app.openapi()["paths"].items():
        for method, op in ops.items():
            if method.upper() in MUTATING and set(op.get("tags", [])) & TAGS:
                out.append((method.upper(), re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)))
    return sorted(out)


def body_of(h, **extra):
    return {
        "effective_from": "2020-01-01",
        "source_reference": "synthetic fixture",
        "population_scope": {"description": "synthetic"},
        "disclaimer": "synthetic",
        "care_activity": f"api_{uuid.uuid4().hex[:8]}",
        "selectors": h.selectors(),
        "sources": h.demo_sources(),
        "rules": h.demo_rules(),
        **extra,
    }


def create_version(client, headers, h, **extra):
    g = client.post(
        "/api/v1/guidelines",
        headers=headers,
        json={"guideline_key": f"api-{uuid.uuid4().hex[:10]}", "name": "API test guideline", "topic": "test"},
    )
    assert g.status_code == 201, g.text
    gid = g.json()["data"]["id"]
    v = client.post(f"/api/v1/guidelines/{gid}/versions", headers=headers, json=body_of(h, **extra))
    assert v.status_code == 201, v.text
    return gid, v.json()["data"]


# ---------------------------------------------------------------------------------------------- RBAC
def test_every_mutating_route_is_forbidden_for_the_observer_before_validation(api_client, auth_headers):
    routes = mutating_routes(api_client.app)
    assert len(routes) >= 15  # guards against the sweep silently covering nothing
    for method, path in routes:
        for body in ({}, {"garbage": True}):  # an invalid body must still give 403, never 422
            resp = api_client.request(method, path, headers=auth_headers("observer"), json=body)
            assert resp.status_code == 403, (method, path, resp.status_code, resp.text[:200])
            assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_unauthenticated_requests_are_401_everywhere(api_client):
    for method, path in mutating_routes(api_client.app):
        assert api_client.request(method, path, json={}).status_code == 401, (method, path)
    assert api_client.get("/api/v1/guidelines").status_code == 401
    assert api_client.get(f"/api/v1/care-gaps/{uuid.uuid4()}").status_code == 401


def test_roles_without_authoring_or_processing_permissions_are_forbidden(api_client, auth_headers, h):
    for role in ("care_coordinator",):  # a coordinator may not author guidelines
        assert api_client.post("/api/v1/guidelines", headers=auth_headers(role), json={}).status_code == 403
        assert (
            api_client.post("/api/v1/rules/validate", headers=auth_headers(role), json={}).status_code == 403
        )
    # reviewers approve but do not run population processing; admins run processing but may not approve
    assert (
        api_client.post(
            "/api/v1/rules/evaluation-jobs", headers=auth_headers("clinical_reviewer"), json={}
        ).status_code
        == 403
    )
    assert (
        api_client.post(
            "/api/v1/care-gaps/generation-jobs", headers=auth_headers("clinical_reviewer"), json={}
        ).status_code
        == 403
    )
    gid, v = create_version(api_client, auth_headers("population_admin"), h)
    for action in ("approve", "retire", "return-to-draft"):
        resp = api_client.post(
            f"/api/v1/guidelines/{gid}/versions/{v['id']}/{action}",
            headers=auth_headers("population_admin"),
            json={},
        )
        assert resp.status_code == 403, action


def test_observers_can_read_everything_relevant(api_client, auth_headers, h):
    gid, v = create_version(api_client, auth_headers("population_admin"), h)
    for path in (
        "/api/v1/guidelines",
        f"/api/v1/guidelines/{gid}",
        f"/api/v1/guidelines/{gid}/versions",
        f"/api/v1/guidelines/{gid}/versions/{v['id']}",
        "/api/v1/rules",
        "/api/v1/evaluations",
        "/api/v1/care-gaps",
    ):
        resp = api_client.get(path, headers=auth_headers("observer"))
        assert resp.status_code == 200, (path, resp.text[:200])


# ---------------------------------------------------------------------------------------------- workflow over
# HTTP
def test_the_review_workflow_over_http_with_envelopes_and_immutability(api_client, auth_headers, h):
    admin, reviewer = auth_headers("population_admin"), auth_headers("clinical_reviewer")
    gid, v = create_version(api_client, admin, h)
    assert (
        v["review_status"] == "draft"
        and v["clinical_review_status"] == "pending_clinical_review"
        and len(v["rules"]) == 5
    )
    assert (
        {r["rule_id"] for r in v["rules"]} >= {"demo.age_min", "demo.record_coverage"}
        and v["selectors"]
        and v["sources"][0]["source_id"] == "synthetic_source"
    )
    base = f"/api/v1/guidelines/{gid}/versions/{v['id']}"
    assert (
        api_client.post(f"{base}/approve", headers=reviewer, json={}).status_code == 409
    )  # draft cannot be approved
    assert api_client.post(f"{base}/submit-review", headers=admin).status_code == 200
    resp = api_client.post(f"{base}/approve", headers=reviewer, json={"clinically_reviewed": True})
    assert resp.status_code == 200 and resp.json()["data"]["review_status"] == "approved"
    assert (
        resp.json()["data"]["clinical_review_status"] == "clinically_reviewed"
        and resp.json()["meta"]["request_id"]
    )
    err = api_client.post(f"{base}/approve", headers=reviewer, json={})
    assert err.status_code == 409 and err.json()["error"]["code"] == "INVALID_TRANSITION"
    rule_id = v["rules"][0]["id"]
    assert (
        api_client.delete(f"/api/v1/rules/{rule_id}", headers=admin).status_code == 409
    )  # approved rules are immutable
    assert (
        api_client.post(
            "/api/v1/rules",
            headers=admin,
            json={
                "guideline_version_id": v["id"],
                "definition": h.demo_rules()[1] | {"rule_id": "demo.late"},
            },
        ).status_code
        == 409
    )
    retired = api_client.post(f"{base}/retire", headers=reviewer, json={"reason": "superseded"})
    assert retired.status_code == 200 and retired.json()["data"]["review_status"] == "retired"
    listing = api_client.get("/api/v1/guidelines?status=retired", headers=reviewer).json()
    assert any(g["id"] == gid for g in listing["data"]) and listing["meta"]["total"] >= 1


def test_invalid_definitions_are_422_with_field_details_and_validate_endpoint_reports_them(
    api_client, auth_headers, h
):
    admin = auth_headers("population_admin")
    g = api_client.post(
        "/api/v1/guidelines",
        headers=admin,
        json={"guideline_key": f"bad-{uuid.uuid4().hex[:8]}", "name": "Bad"},
    ).json()["data"]
    broken = h.demo_rules()
    broken[1]["expression"]["operator"] = "CONTAINS"
    resp = api_client.post(
        f"/api/v1/guidelines/{g['id']}/versions", headers=admin, json=body_of(h, rules=broken)
    )
    assert (
        resp.status_code == 422
        and resp.json()["error"]["code"] == "VALIDATION_ERROR"
        and resp.json()["error"]["details"]
    )
    check = api_client.post(
        "/api/v1/rules/validate", headers=admin, json={"rules": broken, "selectors": h.selectors()}
    ).json()["data"]
    assert check["valid"] is False and check["errors"]
    ok = api_client.post(
        "/api/v1/rules/validate", headers=admin, json={"rules": h.demo_rules(), "selectors": h.selectors()}
    ).json()["data"]
    assert ok["valid"] is True and "demo.age_min" in ok["rules"]
    assert (
        api_client.post(
            f"/api/v1/guidelines/{g['id']}/versions",
            headers=admin,
            json={"effective_from": "2020-01-01", "bogus": 1},
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------------------------- evaluation
# jobs and results
def test_evaluation_jobs_use_approved_versions_only_and_are_idempotent(api_client, auth_headers, h, seed):
    admin = auth_headers("population_admin")
    version, _ = seed.approved_version()
    draft, _ = seed.approved_version(approve=False)
    resident = seed.resident()
    body = {
        "guideline_version_ids": [str(version.id)],
        "evaluation_date": ON.isoformat(),
        "resident_scope": {"type": "residents", "resident_ids": [str(resident.id)]},
        "idempotency_key": f"api-{uuid.uuid4().hex}",
    }
    first = api_client.post("/api/v1/rules/evaluation-jobs", headers=admin, json=body)
    assert first.status_code == 202, first.text
    job = first.json()["data"]
    assert (
        job["job_type"] == "guidelines.evaluate_population"
        and job["status"] == "queued"
        and job["config_version"].startswith("guideline-engine/")
    )
    again = api_client.post("/api/v1/rules/evaluation-jobs", headers=admin, json=body)
    assert again.status_code == 200 and again.json()["data"]["id"] == job["id"]
    unapproved = api_client.post(
        "/api/v1/rules/evaluation-jobs",
        headers=admin,
        json={**body, "idempotency_key": None, "guideline_version_ids": [str(draft.id)]},
    )
    assert unapproved.status_code == 409 and unapproved.json()["error"]["code"] == "CONFLICT"
    bad_scope = api_client.post(
        "/api/v1/rules/evaluation-jobs", headers=admin, json={**body, "resident_scope": {"type": "dataset"}}
    )
    assert bad_scope.status_code == 422


def test_an_evaluation_returns_everything_the_owner_requires(api_client, auth_headers, h, seed):
    version, rules = seed.approved_version()
    young = seed.resident(birth=date(2005, 1, 1))
    ok = seed.resident()
    h.run_evaluation(seed.s, [version], ON)
    observer = auth_headers("observer")
    listing = api_client.get(f"/api/v1/residents/{young.id}/evaluations", headers=observer)
    assert listing.status_code == 200 and listing.json()["meta"]["total"] == 1
    e = listing.json()["data"][0]
    assert api_client.get(f"/api/v1/evaluations/{e['id']}", headers=observer).json()["data"]["id"] == e["id"]
    # guideline + rule identity and versions
    assert (
        e["guideline_version_id"] == str(version.id)
        and e["guideline_version"] == 1
        and e["guideline_key"]
        and e["guideline_id"] == str(version.guideline_id)
    )
    assert (e["rule_id"], e["rule_version"], e["rule_version_id"]) == (
        "demo.age_min",
        1,
        str(rules["age_min"].id),
    )
    # outcome, reason, explanation, review status
    assert (e["outcome"], e["outcome_class"], e["reason_code"], e["review_status"]) == (
        "not_eligible",
        "ineligible",
        "below_minimum_age",
        "pending_clinical_review",
    )
    assert (
        e["explanation"]["criteria"]
        and e["explanation"]["decision"]["decisive_rule_id"] == "demo.age_min"
        and e["care_activity"] == version.care_activity
    )
    # citations, evidence pointers, missing and conflicting information are part of the contract (empty lists
    # when none apply)
    assert e["source_references"][0]["references"][0]["source_id"] == "synthetic_source"
    for key in ("evidence_references", "missing_information", "conflicting_information", "exclusion_reasons"):
        assert isinstance(e[key], list), key
    assert (
        e["execution_version"].startswith("guideline-engine/")
        and e["evaluation_date"] == ON.isoformat()
        and len(e["input_hash"]) == 64
    )
    eligible = api_client.get(f"/api/v1/residents/{ok.id}/evaluations", headers=observer).json()["data"][0]
    assert (
        eligible["outcome"] == "eligible"
        and eligible["rule_version_id"] is None
        and eligible["rule_id"] is None
    )
    filtered = api_client.get(
        f"/api/v1/evaluations?outcome=not_eligible&resident_id={young.id}", headers=observer
    ).json()
    assert filtered["meta"]["total"] == 1
    assert api_client.get("/api/v1/evaluations?outcome=bogus", headers=observer).status_code == 422
    assert api_client.get(f"/api/v1/evaluations/{uuid.uuid4()}", headers=observer).status_code == 404
    assert (
        api_client.get(f"/api/v1/residents/{uuid.uuid4()}/evaluations", headers=observer).status_code == 404
    )


def test_approved_versions_are_not_editable_through_the_api_even_by_a_reviewer(
    api_client, auth_headers, h, seed
):
    version, rules = seed.approved_version()
    resp = api_client.delete(
        f"/api/v1/rules/{rules['age_min'].id}", headers=auth_headers("clinical_reviewer")
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "INVALID_TRANSITION"
    assert seed.s.get(GuidelineVersion, version.id).review_status == "approved"
