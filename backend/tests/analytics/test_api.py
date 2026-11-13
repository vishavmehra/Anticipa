"""Analytics API contract (integration, DB `anticipa_test`, rolled back): envelope, authentication, RBAC per role,
observer read-only access, GET-only routes, parameter validation and the guarantee that no endpoint writes anything."""
# ruff: noqa: E501

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

import anticipa.models  # noqa: F401  (registers every module's tables on Base.metadata)
from anticipa.analytics.routes import router
from anticipa.core import auth as core_auth
from anticipa.db import Base

API = "/api/v1"
ALL_ROLES = ("population_admin", "care_coordinator", "clinical_reviewer", "system_admin", "observer")


def paths(resident_id, case_id):
    return {
        "command-center": "/analytics/command-center",
        "funnel": "/analytics/funnel",
        "processing": "/analytics/processing",
        "geography": "/analytics/geography",
        "programmes": "/analytics/programmes",
        "explorer": "/analytics/population-explorer",
        "alerts": "/analytics/alerts/summary",
        "scope": "/meta/scope",
        "profile": f"/residents/{resident_id}/profile",
        "journey": f"/residents/{resident_id}/journey",
        "pedigree": f"/residents/{resident_id}/pedigree",
        "preview": f"/engagement/preview?case_id={case_id}",
    }


# who may read what (rbac.py): analytics.view / analytics.view_limited, residents.view, engagement.view, and system.configure
# (system_admin only) for the internal processing monitor
EXPECTED = {
    "system_admin": dict.fromkeys(paths(0, 0), 200),
    "population_admin": {**dict.fromkeys(paths(0, 0), 200), "processing": 403},
    "observer": {**dict.fromkeys(paths(0, 0), 200), "processing": 403},
    "care_coordinator": {**dict.fromkeys(paths(0, 0), 200), "processing": 403, "geography": 403, "explorer": 403},  # analytics.view_limited only
    "clinical_reviewer": {**dict.fromkeys(paths(0, 0), 200), "processing": 403, "preview": 403},  # no engagement.view
}


def test_unauthenticated_and_roleless_callers_are_rejected(api_client_nodb, mint_token):
    for path in paths(uuid.uuid4(), uuid.uuid4()).values():
        r = api_client_nodb.get(f"{API}{path}")
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED", path
        r = api_client_nodb.get(f"{API}{path}", headers={"Authorization": f"Bearer {mint_token(None)}"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", path
        r = api_client_nodb.get(f"{API}{path}", headers={"Authorization": f"Bearer {mint_token('not_a_role')}"})
        assert r.status_code == 403, path


@pytest.mark.parametrize("role", ALL_ROLES)
def test_role_matrix(api_client, auth_headers, make_case, role):
    case = make_case("planned")
    for name, path in paths(case.resident_id, case.id).items():
        r = api_client.get(f"{API}{path}", headers=auth_headers(role))
        assert r.status_code == EXPECTED[role][name], (role, name, r.text)
        if r.status_code == 200:
            body = r.json()
            assert set(body) == {"data", "meta"} and body["meta"]["request_id"]
        else:
            assert r.json()["error"]["code"] == "FORBIDDEN"


def test_every_route_is_get_only_and_rejects_other_methods(api_client, auth_headers, make_case):
    case = make_case("planned")
    assert {m for route in router.routes for m in route.methods - {"HEAD"}} == {"GET"}
    h = auth_headers("population_admin")  # even the most privileged role cannot change anything through these routes
    for path in paths(case.resident_id, case.id).values():
        for method in ("post", "put", "patch", "delete"):
            r = api_client.request(method, f"{API}{path}", headers=h, json={})
            assert r.status_code == 405, (method, path)


def test_parameter_validation(api_client, auth_headers):
    h = auth_headers("observer")
    for days in (0, 91, "x"):
        assert api_client.get(f"{API}/analytics/command-center?days={days}", headers=h).status_code == 422
    assert api_client.get(f"{API}/analytics/command-center?days=7", headers=h).json()["data"]["closure_trend"]["days"] == 7


def table_counts(session) -> dict[str, int]:
    return {t.name: session.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}


def test_only_resident_reads_write_and_only_an_audit_event(api_client, auth_headers, make_case, seed, world, db_session, monkeypatch):
    # the auth layer's own last-seen upsert is not part of the analytics code; stub it so *every* table is compared
    monkeypatch.setattr(core_auth, "touch_profile", lambda *args, **kwargs: None)
    case = make_case("planned")
    resident = seed.resident()
    seed.document(resident)
    seed.fact(resident, fact_type="breast_cancer")
    db_session.flush()
    before = table_counts(db_session)
    assert len(before) > 25 and before["care_gap_cases"] == 1
    resident_reads = 0
    for role in ("observer", "population_admin", "care_coordinator"):
        for name, path in {**paths(case.resident_id, case.id), "seeded-journey": f"/residents/{resident.id}/journey",
                           "seeded-pedigree": f"/residents/{resident.id}/pedigree"}.items():  # fmt: skip
            r = api_client.get(f"{API}{path}", headers=auth_headers(role))
            assert r.status_code in (200, 403), (role, name, r.text)
            resident_reads += r.status_code == 200 and name in ("journey", "pedigree", "profile", "seeded-journey", "seeded-pedigree")
    after = table_counts(db_session)
    assert resident_reads == 15
    assert after.pop("audit_events") - before.pop("audit_events") == resident_reads  # one resident.read each, nothing else
    assert after == before  # no other table changed


def test_resident_reads_are_audited_like_get_resident(api_client, auth_headers, seed, db_session):
    from anticipa.core.models import AuditEvent

    r = seed.resident()
    h = auth_headers("observer")
    for view in ("journey", "pedigree"):
        assert api_client.get(f"{API}/residents/{r.id}/{view}", headers=h).status_code == 200
    assert api_client.get(f"{API}/residents/{uuid.uuid4()}/journey", headers=h).status_code == 404  # a 404 is not audited
    rows = db_session.execute(select(AuditEvent).where(AuditEvent.resource_id == str(r.id)).order_by(AuditEvent.occurred_at, AuditEvent.id)).scalars().all()
    assert sorted(e.details["view"] for e in rows) == ["journey", "pedigree"]
    assert {(e.action, e.resource_type, e.outcome, e.actor_role) for e in rows} == {("resident.read", "resident", "success", "observer")}
    assert all(e.actor_id is not None for e in rows)
    assert db_session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.resource_id.is_(None), AuditEvent.action == "resident.read")) == 0
    # the Command Center's activity feed hides read events, so these never show up there
    items = api_client.get(f"{API}/analytics/command-center", headers=h).json()["data"]["recent_activity"]["items"]
    assert "resident.read" not in [i["title"] for i in items]
