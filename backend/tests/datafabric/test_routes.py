"""Resident / record / family / timeline / population-overview APIs against the test database."""
# ruff: noqa: E501

from __future__ import annotations

import importlib
import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from anticipa.core.models import AuditEvent
from anticipa.datafabric.family import link_ready_blocks
from anticipa.datafabric.loader import ingest_dataset
from anticipa.datafabric.models import ClinicalRecord, Dataset, Document, FamilyEdge, Resident
from anticipa.datafabric.routes import AGE_BANDS, age_band, age_on, band_birth_range
from anticipa.main import create_app

V = "syn-20261108-s7"
N = 300
API = "/api/v1"


@pytest.fixture
def api_client(test_settings, test_engine, db_session, jwks, monkeypatch):
    """Like the shared `api_client`, but mounts only this module's routes so another module's broken import cannot hide
    (or cause) failures here."""
    monkeypatch.setattr(
        "anticipa.api.router.load_module_files",
        lambda filename: (
            [importlib.import_module("anticipa.datafabric.routes")] if filename == "routes" else []
        ),
    )
    app = create_app(test_settings, engine=test_engine)
    app.state.sessionmaker = sessionmaker(
        bind=db_session.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def loaded(db_session, fhir, tmp_path):
    fhir.generate(tmp_path, residents=N, batch_size=150)
    ingest_dataset(db_session, V, data_dir=tmp_path)
    link_ready_blocks(db_session, V, block_batches=2, data_dir=tmp_path, gt_root=tmp_path / "gt")
    db_session.commit()
    return db_session.execute(select(Dataset).where(Dataset.dataset_version == V)).scalar_one()


def _get(client, headers, path, role="observer", **params):
    return client.get(f"{API}{path}", headers=headers(role), params=params)


def test_requires_authentication(api_client):
    assert api_client.get(f"{API}/residents").status_code == 401
    assert api_client.get(f"{API}/analytics/population-overview").status_code == 401


def test_list_residents_pagination_and_envelope(api_client, auth_headers, loaded):
    body = _get(api_client, auth_headers, "/residents", dataset_version=V, page_size=100).json()
    assert (
        body["meta"]["total"] == N and body["meta"]["page_size"] == 100 and body["meta"]["total_pages"] == 3
    )
    assert len(body["data"]) == 100 and body["meta"]["request_id"]
    r = body["data"][0]
    assert (
        r["dataset_version"] == V and r["synthetic_status"] == "synthetic" and r["age_as_of"] == "2026-11-08"
    )
    assert r["age_band"] == age_band(r["age"])
    last = _get(api_client, auth_headers, "/residents", dataset_version=V, page_size=100, page=3).json()
    assert len(last["data"]) == 100 and last["data"][0]["id"] != r["id"]
    assert _get(api_client, auth_headers, "/residents", page_size=500).status_code == 422


def test_list_filters(api_client, auth_headers, loaded):
    def ids(**p):
        return _get(api_client, auth_headers, "/residents", page_size=200, **p).json()

    by_district = ids(district="d03")
    assert by_district["meta"]["total"] > 0 and {r["district_code"] for r in by_district["data"]} == {"D03"}
    female = ids(sex="female")
    assert female["meta"]["total"] > 0 and {r["administrative_sex"] for r in female["data"]} == {"female"}
    hindi = ids(language="HI")
    assert {r["preferred_language"] for r in hindi["data"]} == {"hi"}
    band = ids(age_band="50-59")
    assert band["meta"]["total"] > 0 and all(50 <= r["age"] <= 59 for r in band["data"])
    senior = ids(age_band="80+")
    assert all(r["age"] >= 80 for r in senior["data"])
    combo = ids(district="D03", sex="female", age_band="30-39")
    assert all(
        r["district_code"] == "D03" and r["administrative_sex"] == "female" and 30 <= r["age"] <= 39
        for r in combo["data"]
    )
    one = ids()["data"][0]
    assert ids(search=one["id"])["data"][0]["id"] == one["id"]
    assert ids(search=one["source_patient_id"][:12])["meta"]["total"] >= 1
    assert ids(search="zzzzzz-nothing")["meta"]["total"] == 0
    assert _get(api_client, auth_headers, "/residents", age_band="200+").status_code == 422
    assert _get(api_client, auth_headers, "/residents", sex="robot").status_code == 422
    assert (
        _get(api_client, auth_headers, "/residents", dataset_version="syn-20990101-s1").json()["meta"][
            "total"
        ]
        == 0
    )


def test_age_band_ranges_partition_all_ages():
    as_of = date(2026, 11, 8)
    for age in range(0, 110):
        label = age_band(age)
        earliest, latest = band_birth_range(label, as_of)
        birth = as_of.replace(
            year=as_of.year - age
        )  # birthday today: exactly `age` years old, inside the band's window
        assert age_on(birth, as_of) == age and birth <= latest and (earliest is None or birth >= earliest)
    assert [b[0] for b in AGE_BANDS][0] == "0-17"
    assert age_on(date(2000, 11, 9), as_of) == 25 and age_on(date(2000, 11, 8), as_of) == 26


def test_resident_detail_and_not_found(api_client, auth_headers, db_session, loaded):
    rid = _get(api_client, auth_headers, "/residents").json()["data"][0]["id"]
    d = _get(api_client, auth_headers, f"/residents/{rid}").json()["data"]
    assert d["record_counts"] == {
        "condition": 1,
        "diagnostic_report": 1,
        "encounter": 1,
        "immunization": 1,
        "medication": 1,
        "observation": 2,
        "procedure": 1,
    }
    assert "Synthetic" in d["notice"] and d["document_count"] == 0
    assert (
        _get(api_client, auth_headers, "/residents/00000000-0000-0000-0000-000000000000").status_code == 404
    )
    assert _get(api_client, auth_headers, "/residents/not-a-uuid").status_code == 422
    audit_n = db_session.execute(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "resident.read", AuditEvent.resource_id == rid)
    ).scalar_one()
    assert audit_n == 1  # reads of a resident are audited


def test_records_filters_and_ordering(api_client, auth_headers, loaded):
    rid = _get(api_client, auth_headers, "/residents").json()["data"][0]["id"]
    allrec = _get(api_client, auth_headers, f"/residents/{rid}/records", page_size=50).json()
    assert allrec["meta"]["total"] == 8
    starts = [r["effective_start"] for r in allrec["data"]]
    assert starts == sorted(starts, reverse=True)  # newest first
    obs = _get(api_client, auth_headers, f"/residents/{rid}/records", category="observation").json()
    assert obs["meta"]["total"] == 2 and {r["category"] for r in obs["data"]} == {"observation"}
    bmi = _get(api_client, auth_headers, f"/residents/{rid}/records", tag="bmi").json()["data"]
    assert len(bmi) == 1 and bmi[0]["value"]["unit"] == "kg/m2" and bmi[0]["code"] == "39156-5"
    assert bmi[0]["source_reference"].startswith(f"synthea://{V}/batch-")
    by_code = _get(api_client, auth_headers, f"/residents/{rid}/records", code="71651007").json()
    assert by_code["meta"]["total"] == 1 and by_code["data"][0]["display"] == "Mammography"
    future = _get(api_client, auth_headers, f"/residents/{rid}/records", since="2030-01-01T00:00:00Z").json()
    assert future["meta"]["total"] == 0
    assert _get(api_client, auth_headers, f"/residents/{rid}/records", page=0).status_code == 422


def test_family_endpoint(api_client, auth_headers, db_session, loaded):
    linked = db_session.execute(select(FamilyEdge.resident_id).limit(1)).scalar_one()
    f = _get(api_client, auth_headers, f"/residents/{linked}/family").json()["data"]
    assert f["resident_id"] == str(linked) and f["edges"] and "Synthetic" in f["notice"]
    e = f["edges"][0]
    assert e["relationship_direction"] == "related_to_resident" and e["source_type"] == "deterministic_linker"
    assert e["related_resident"]["id"] and e["related_resident"]["age"] is not None
    assert sum(f["counts_by_relationship"].values()) == len(f["edges"])
    lonely = db_session.execute(
        select(Resident.id).where(~Resident.id.in_(select(FamilyEdge.resident_id))).limit(1)
    ).scalar_one()
    assert _get(api_client, auth_headers, f"/residents/{lonely}/family").json()["data"]["edges"] == []


def test_timeline_merges_records_and_documents(api_client, auth_headers, db_session, loaded):
    rid = _get(api_client, auth_headers, "/residents").json()["data"][0]["id"]
    db_session.add(
        Document(
            resident_id=uuid.UUID(rid),
            document_type="discharge_summary",
            storage_key="k/1",
            content_hash="h1",
            created_on=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )
    db_session.commit()
    tl = _get(api_client, auth_headers, f"/residents/{rid}/timeline", page_size=50).json()
    assert tl["meta"]["total"] == 9
    assert (
        tl["data"][0]["source"] == "document" and tl["data"][0]["kind"] == "discharge_summary"
    )  # newest first
    times = [e["occurred_at"] for e in tl["data"]]
    assert times == sorted(times, reverse=True)
    only_docs = _get(api_client, auth_headers, f"/residents/{rid}/timeline", kind="discharge_summary").json()
    assert only_docs["meta"]["total"] == 1
    # care coordinators can read records but hold no documents.view: their timeline has no document events
    cc = _get(
        api_client, auth_headers, f"/residents/{rid}/timeline", role="care_coordinator", page_size=50
    ).json()
    assert cc["meta"]["total"] == 8 and all(e["source"] == "clinical_record" for e in cc["data"])


def test_population_overview_counts_and_definitions(api_client, auth_headers, db_session, loaded):
    d = _get(api_client, auth_headers, "/analytics/population-overview").json()["data"]
    assert (
        d["dataset_version"] == V
        and d["reference_date"] == "2026-11-08"
        and "Synthetic" in d["notice"]
        and not d["limited"]
    )
    m = {x["key"]: x for x in d["metrics"]}
    assert m["residents_total"]["value"] == N
    assert all(x["definition"] for x in d["metrics"]) and all(x["definition"] for x in d["distributions"])
    assert m["clinical_records_total"]["value"] == N * 8
    assert m["clinical_records_observation"]["value"] == N * 2 and m["documents_total"]["value"] == 0
    edges = db_session.execute(
        select(func.count())
        .select_from(FamilyEdge)
        .join(Resident, Resident.id == FamilyEdge.resident_id)
        .where(Resident.dataset_id == loaded.id)
    ).scalar_one()
    assert m["family_edges_total"]["value"] == edges and 0 < m["residents_with_family_links"]["value"] < N
    dist = {x["key"]: x for x in d["distributions"]}
    assert set(dist) == {"age_band", "sex", "district", "language"}
    for x in dist.values():
        assert (
            sum(b["count"] for b in x["buckets"]) == N
            and abs(sum(b["share"] for b in x["buckets"]) - 1) < 0.01
        )
    assert [b["key"] for b in dist["age_band"]["buckets"]] == sorted(
        b["key"] for b in dist["age_band"]["buckets"]
    )
    assert d["basis"]["clinical_records"].startswith("sum of per-batch")
    # the limited analytics role gets totals and age/sex only
    lim = _get(api_client, auth_headers, "/analytics/population-overview", role="care_coordinator").json()[
        "data"
    ]
    assert lim["limited"] and {x["key"] for x in lim["distributions"]} == {"age_band", "sex"}
    assert {x["key"] for x in lim["metrics"]} == {
        "residents_total",
        "residents_deceased",
        "residents_full_profile",  # the storage-profile split is stated for every audience
        "residents_rules_profile",
    }
    assert (
        _get(
            api_client, auth_headers, "/analytics/population-overview", dataset_version="syn-20990101-s1"
        ).status_code
        == 404
    )


def test_every_role_with_view_permissions_can_read(api_client, auth_headers, loaded):
    for role in ("observer", "population_admin", "clinical_reviewer", "system_admin", "care_coordinator"):
        assert _get(api_client, auth_headers, "/residents", role=role).status_code == 200, role
    assert api_client.get(f"{API}/residents", headers=auth_headers("not_a_role")).status_code == 403


def test_reads_do_not_change_clinical_data(api_client, auth_headers, db_session, loaded):
    before = db_session.execute(select(func.count()).select_from(ClinicalRecord)).scalar_one()
    rid = _get(api_client, auth_headers, "/residents").json()["data"][0]["id"]
    for path in ("", "/records", "/family", "/timeline"):
        _get(api_client, auth_headers, f"/residents/{rid}{path}")
    assert db_session.execute(select(func.count()).select_from(ClinicalRecord)).scalar_one() == before
    assert (
        api_client.post(f"{API}/residents", headers=auth_headers("system_admin"), json={}).status_code == 405
    )
