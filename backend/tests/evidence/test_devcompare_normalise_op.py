# ruff: noqa: E501
"""`evidence.devcompare` (dev-split pin, 400-document ceiling, seeded sample) and `ops normalise` (idempotent
re-application, model originals preserved under value["normaliser"], version recorded). No model call anywhere."""

from __future__ import annotations

import argparse
import json
import types
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from anticipa.datafabric.models import Document
from anticipa.evidence import devcompare, ops, service
from anticipa.evidence import normalise as nm
from anticipa.evidence.cohort import save_manifest, select_cohort
from anticipa.evidence.models import EvidenceFact, EvidenceSource

# ------------------------------------------------------------------------------------------ devcompare (unit)


def _info(n):
    kinds = [("clinical_note", "en"), ("lab_report", "hi"), ("referral_letter", "ar")]
    return {
        f"d{i:03d}": types.SimpleNamespace(document_type=kinds[i % 3][0], language=kinds[i % 3][1])
        for i in range(n)
    }


def test_sample_is_seeded_stratified_and_never_larger_than_asked():
    info = _info(90)
    a = devcompare.pick_sample(sorted(info), info, 30, seed=7)
    assert a == devcompare.pick_sample(sorted(info), info, 30, seed=7)  # reproducible
    assert a != devcompare.pick_sample(sorted(info), info, 30, seed=8)
    assert len(a) == 30 and a == sorted(a)
    strata = [(info[d].document_type, info[d].language) for d in a]
    assert {strata.count(k) for k in set(strata)} == {10} and len(
        set(strata)
    ) == 3  # round-robin: equal share per stratum
    assert len(devcompare.pick_sample(sorted(info), info, 500, seed=7)) == 90  # smaller population: all of it


def test_more_than_the_approved_400_documents_is_refused_before_anything_is_loaded(monkeypatch):
    monkeypatch.setattr(
        devcompare.readers, "load_cohort_manifest", lambda p: pytest.fail("loaded despite refusal")
    )
    with pytest.raises(SystemExit, match="at most 400"):
        devcompare.main(["--dataset-version", "v", "--cohort-manifest", "m.json", "--docs", "401"])


def test_ground_truth_is_only_ever_loaded_for_the_dev_split(monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def fake_load(dataset_version, **kw):
        seen.update(kw)
        raise Stop

    monkeypatch.setattr(devcompare.readers, "load_cohort_manifest", lambda p: {"resident_ids": ["r1"]})
    monkeypatch.setattr(devcompare.gtmod, "load_ground_truth", fake_load)
    with pytest.raises(Stop):
        devcompare.main(
            ["--dataset-version", "v", "--cohort-manifest", "m.json", "--docs", "400", "--no-checksum"]
        )
    assert seen["split"] == "dev" and seen["verify"] is False
    assert seen["resident_ids"] == ["r1"]
    with pytest.raises(SystemExit):  # there is no way to ask for another split
        devcompare.main(["--dataset-version", "v", "--cohort-manifest", "m.json", "--split", "test"])


# ------------------------------------------------------------------------------------------ ops normalise (integration)
DOC_DATE = datetime(2026, 6, 1, tzinfo=UTC)
TEXT = "Discharge note\nPast medical history\nHypertension.\nPlan\nBP 150/95 mmHg. Colonoscopy done."


class Store:
    def __init__(self):
        self.texts: dict[str, str] = {}

    def read_text(self, key):
        if key not in self.texts:
            raise FileNotFoundError(key)
        return self.texts[key]

    def write_text(self, key, text):
        self.texts[key] = text
        return key

    def exists(self, key):
        return key in self.texts


@pytest.fixture
def setup_cohort(world, sessions, monkeypatch):
    store = Store()
    monkeypatch.setattr(service, "get_store", lambda: store)
    monkeypatch.setattr(ops, "get_sessionmaker", lambda: sessions)
    rid = world.resident(preferred_language="en")
    did = world.document(rid, TEXT)
    store.texts[f"{world.tag}/{did}.txt"] = TEXT
    with sessions() as s:
        s.execute(update(Document).where(Document.id == did).values(created_on=DOC_DATE))
        s.commit()
        manifest = select_cohort(s, seed=1, size=10, dataset_version=f"test-{world.tag}")
    save_manifest(store, manifest)
    return manifest.cohort_id, rid, did


def _add(
    sessions,
    rid,
    did,
    *,
    category,
    temporal="current",
    assertion="positive",
    fact_type="x",
    value=None,
    start=None,
    review="not_required",
    subject="resident",
):
    fid = uuid.uuid4()
    with sessions() as s:
        s.add(
            EvidenceFact(
                id=fid,
                resident_id=rid,
                subject_type=subject,
                category=category,
                fact_type=fact_type,
                value=value or {},
                assertion_status=assertion,
                temporal_status=temporal,
                validation_status="source_verified",
                review_status=review,
                extraction_method="llm",
                model_provider="anthropic",
                model_id="m",
                prompt_version="p@1",
            )
        )
        s.flush()
        s.add(
            EvidenceSource(
                evidence_fact_id=fid,
                document_id=did,
                source_quote="q",
                source_start_offset=start,
                source_end_offset=None if start is None else start + 1,
                verification_status="verified_exact",
            )
        )
        s.commit()
    return fid


def _row(sessions, fid):
    with sessions() as s:
        f = s.get(EvidenceFact, fid)
        return f.category, f.temporal_status, f.value


def _run(cohort_id, capsys, dry_run=False):
    capsys.readouterr()
    assert ops.cmd_normalise(argparse.Namespace(cohort_id=cohort_id, dry_run=dry_run)) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.integration
def test_normalise_is_idempotent_preserves_the_models_originals_and_records_the_version(
    world, sessions, setup_cohort, monkeypatch, capsys
):
    cohort_id, rid, did = setup_cohort
    v0 = nm.NORMALISER_VERSION
    bp = _add(
        sessions,
        rid,
        did,
        category="diagnostic_finding",
        value={"concept": "blood pressure", "unit": "mmHg", "numeric_value": 150},
    )
    proc = _add(
        sessions,
        rid,
        did,
        category="procedure",
        fact_type="colonoscopy",
        value={"concept": "colonoscopy", "event_date": "2020-03-01"},
    )
    htn = _add(
        sessions,
        rid,
        did,
        category="condition",
        fact_type="hypertension",
        value={"concept": "hypertension"},
        start=TEXT.index("Hypertension"),
    )
    human = _add(
        sessions,
        rid,
        did,
        category="diagnostic_finding",
        value={"concept": "blood pressure", "numeric_value": 150},
        review="accepted",
    )
    same = _add(
        sessions,
        rid,
        did,
        category="family_history",
        temporal="historical",
        subject="family_member",
        value={"concept": "asthma"},
    )

    dry = _run(cohort_id, capsys, dry_run=True)
    assert dry["facts_changed"] == 3 and dry["dry_run"] is True
    assert _row(sessions, bp)[0] == "diagnostic_finding"  # a dry run writes nothing

    out = _run(cohort_id, capsys)
    assert out["facts_changed"] == 3 and out["version"] == nm.NORMALISER_VERSION
    assert out["rules"] == {"C1:measurement_shape": 1, "T1:event_before_document": 1, "T2:history_section": 1}

    cat, temp, val = _row(sessions, bp)
    assert (cat, temp) == ("observation", "current")
    assert val["normaliser"] == {
        "version": nm.NORMALISER_VERSION,
        "rules": ["C1:measurement_shape"],
        "original": {"category": "diagnostic_finding", "temporal_status": "current"},
    }
    assert val["numeric_value"] == 150  # the model's own value fields are untouched
    assert (
        _row(sessions, proc)[1] == "historical"
        and _row(sessions, proc)[2]["normaliser"]["original"]["temporal_status"] == "current"
    )
    assert _row(sessions, htn)[1] == "historical"
    assert (
        _row(sessions, human)[0] == "diagnostic_finding" and "normaliser" not in _row(sessions, human)[2]
    )  # human-reviewed: never touched
    assert "normaliser" not in _row(sessions, same)[2]  # unchanged facts get no audit block

    before = {f: _row(sessions, f) for f in (bp, proc, htn, human, same)}
    again = _run(cohort_id, capsys)
    assert again["facts_changed"] == 0  # idempotent
    assert {f: _row(sessions, f) for f in before} == before

    monkeypatch.setattr(nm, "NORMALISER_VERSION", "n9")  # a later version re-derives from the originals
    newer = _run(cohort_id, capsys)
    assert newer["facts_changed"] == 3 and newer["version"] == "n9"
    cat, _, val = _row(sessions, bp)
    assert cat == "observation" and val["normaliser"]["version"] == "n9"
    assert val["normaliser"]["original"] == {
        "category": "diagnostic_finding",
        "temporal_status": "current",
    }  # still the model's labels, not n2's

    with sessions() as s:  # the audit log carries the version
        from anticipa.core.models import AuditEvent

        ev = (
            s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "evidence.normalise", AuditEvent.resource_id == cohort_id
                )
            )
            .scalars()
            .all()
        )
    assert sorted(e.details["version"] for e in ev) == sorted([v0, "n9"]) and all(
        e.details["facts_changed"] == 3 for e in ev
    )
