"""Guideline Studio dry run (anticipa.guidelines.dryrun + the dry-run routes and job). Integration: DB
`anticipa_test`, rolled back. The headline property is that a dry run NEVER persists: evaluations and
care gap cases are unchanged afterwards, and approved versions are untouched."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select, text

from anticipa.caregaps.models import CareGapCase
from anticipa.datafabric.docstore import LocalDocumentStore
from anticipa.datafabric.models import Resident
from anticipa.evidence import service as evidence_service
from anticipa.evidence.cohort import CohortManifest, ids_sha256, save_manifest
from anticipa.guidelines import dryrun, service
from anticipa.guidelines.handlers import handle_dry_run
from anticipa.guidelines.models import EligibilityEvaluation, Guideline
from anticipa.jobs import service as jobs
from anticipa.jobs.context import JobContext

ON = date(2026, 11, 8)


@pytest.fixture
def h(seed_module):
    return seed_module


def widened_rules(h, age_min: int = 30):
    """The demo rules with the minimum age lowered: residents aged 30 to 39 become due."""
    rules = copy.deepcopy(h.demo_rules())
    rules[1]["expression"]["value"] = age_min  # demo.age_min
    return rules


class World:
    """An approved v1 (age 40 to 70), a draft v2 (age 30 to 70) and five residents."""

    def __init__(self, h, seed, db_session):
        self.h, self.s = h, db_session
        self.v1, _ = seed.approved_version()
        guideline = self.s.get(Guideline, self.v1.guideline_id)
        self.v2, _ = seed.approved_version(
            widened_rules(h), guideline=guideline, approve=False, care_activity=self.v1.care_activity
        )
        self.young = seed.resident(birth=date(2001, 1, 1))  # 25
        self.d1 = seed.resident(birth=date(1991, 1, 1), district="D01")  # 35: newly due
        self.d2 = seed.resident(birth=date(1991, 6, 1), district="D02")  # 35: newly due
        self.mid = seed.resident(birth=date(1981, 1, 1))  # 45: due under both
        self.old = seed.resident(birth=date(1951, 1, 1))  # 75: not due under either
        self.dataset = seed.dataset.dataset_version
        self.ids = [r.id for r in (self.young, self.d1, self.d2, self.mid, self.old)]

    def scope(self, **over):
        base = {"version_id": self.v2.id, "sample": "random_n", "evaluation_date": ON, "n": 100}
        base["dataset_version"] = self.dataset
        return dryrun.DryRunScope(**{**base, **over})

    def run(self, **over):
        return dryrun.run_dry_run(self.h.session_factory(self.s), self.scope(**over))

    def row_counts(self) -> tuple[int, int]:
        mine = select(Resident.id).where(Resident.dataset_id == self.h.active_dataset_id())
        return (
            self.s.execute(
                select(func.count())
                .select_from(EligibilityEvaluation)
                .where(EligibilityEvaluation.resident_id.in_(mine))
            ).scalar_one(),
            self.s.execute(
                select(func.count()).select_from(CareGapCase).where(CareGapCase.resident_id.in_(mine))
            ).scalar_one(),
        )


@pytest.fixture
def world(h, seed, db_session):
    return World(h, seed, db_session)


# ---------------------------------------------------------------------------------------------- the result
def test_dry_run_reports_the_population_impact_against_the_approved_version(world):
    result = world.run()
    assert result["persisted"] is False
    assert result["version"]["version"] == 2 and result["version"]["review_status"] == "draft"
    assert result["baseline"]["version"] == 1 and result["baseline"]["review_status"] == "approved"
    assert result["sample"]["evaluated"] == 5 and result["sample"]["population_residents"] == 5
    assert result["outcomes"] == {"eligible": 3, "not_eligible": 2}
    assert result["baseline_outcomes"] == {"eligible": 1, "not_eligible": 4}
    diff = result["diff"]
    assert (diff["newly_due"], diff["no_longer_due"], diff["unchanged"]) == (2, 0, 3)
    assert diff["eligible_baseline"] == 1 and diff["eligible_version"] == 3
    assert diff["newly_due_by_district"] == {"D01": 1, "D02": 1}
    assert diff["newly_due_by_age_band"] == {"30-39": 2}
    assert diff["transitions"] == [{"from": "not_eligible", "to": "eligible", "count": 2}]
    assert result["by_district"]["D01"] == {"eligible": 1} and result["by_district"]["D1"] == {
        "eligible": 1,
        "not_eligible": 2,
    }
    assert result["by_sex"] == {"female": {"eligible": 3, "not_eligible": 2}}
    assert result["by_age_band"]["30-39"] == {"eligible": 2}
    reasons = {(r["outcome"], r["reason_code"]): r for r in result["top_reason_codes"]}
    assert reasons[("not_eligible", "above_maximum_age")]["count"] == 1
    assert reasons[("not_eligible", "below_minimum_age")]["count"] == 1  # 25 year old; was 3 under v1
    assert reasons[("not_eligible", "below_minimum_age")]["baseline_count"] == 3
    ex = result["examples"]
    assert [e["change"] for e in ex[:2]] == ["newly_due", "newly_due"]
    assert {e["resident_id"] for e in ex[:2]} == {str(world.d1.id), str(world.d2.id)}
    assert len(ex) == 5 and all(set(e) >= {"resident_id", "outcome", "reason_code", "reason"} for e in ex)
    assert result["runtime_seconds"] >= 0 and result["residents_with_errors"] == 0
    assert result["estimate"]["population_residents"] == 5  # random sample covered the whole population


def test_a_dry_run_writes_nothing_and_never_touches_the_approved_version(world, h):
    h.run_evaluation(world.s, [world.v1], ON)  # real evaluations exist, so "unchanged" is meaningful
    before = world.row_counts()
    assert before[0] == 5
    approved_hash, approved_status = world.v1.content_hash, world.v1.review_status
    draft_hash = world.v2.content_hash
    world.run()
    world.run(n=2)
    assert world.row_counts() == before  # no evaluations, no cases
    world.s.refresh(world.v1)
    world.s.refresh(world.v2)
    assert (world.v1.content_hash, world.v1.review_status) == (approved_hash, approved_status)
    assert (world.v2.content_hash, world.v2.review_status) == (draft_hash, "draft")


def test_viewing_an_approved_version_compares_it_with_the_other_approved_version(world, h):
    service.submit_for_review(world.s, h.REVIEWER, world.v2)
    service.approve(world.s, h.REVIEWER, world.v2, clinically_reviewed=False)
    result = world.run(version_id=world.v1.id)  # v1 judged against v2, the other version in force
    assert result["baseline"]["version"] == 2 and result["diff"]["no_longer_due"] == 2


def test_a_guideline_with_no_other_approved_version_has_no_diff(h, seed, db_session):
    v, _ = seed.approved_version(approve=False)
    seed.resident()
    result = dryrun.run_dry_run(
        h.session_factory(db_session),
        dryrun.DryRunScope(v.id, "random_n", ON, 10, dataset_version=seed.dataset.dataset_version),
    )
    assert result["baseline"] is None and result["diff"] is None and result["baseline_outcomes"] is None
    assert any("nothing to compare" in n for n in result["notes"])


def test_the_random_sample_is_seeded_and_a_smaller_n_is_a_prefix(world):
    s = world.s
    full, _ = dryrun.resolve_sample(s, world.scope(n=5, seed=7))
    again, _ = dryrun.resolve_sample(s, world.scope(n=5, seed=7))
    smaller, population = dryrun.resolve_sample(s, world.scope(n=3, seed=7))
    other, _ = dryrun.resolve_sample(s, world.scope(n=5, seed=8))
    assert full == again and smaller == full[:3] and population == 5
    assert sorted(full) == sorted(world.ids) and full != other  # a different seed orders differently


def test_the_district_filter_narrows_the_sample(world):
    result = world.run(district="D01")
    assert result["sample"]["evaluated"] == 1 and result["diff"]["newly_due"] == 1
    assert result["sample"]["population_residents"] == 1


def test_an_empty_sample_and_unknown_inputs_are_rejected(world):
    with pytest.raises(Exception, match="sample is empty"):
        dryrun.check_scope(world.s, world.scope(district="D99"))
    with pytest.raises(Exception, match="not found"):
        dryrun.check_scope(world.s, world.scope(dataset_version="syn-does-not-exist"))
    with pytest.raises(Exception, match="n is required"):
        dryrun.check_scope(world.s, world.scope(n=None))
    with pytest.raises(Exception, match="does not exist"):
        dryrun.check_scope(world.s, world.scope(sample="ai_cohort", cohort_id="ai-no-such-cohort"))


def test_the_ai_cohort_sample_uses_the_manifest_residents(world, tmp_path, monkeypatch):
    store = LocalDocumentStore(tmp_path)
    monkeypatch.setattr(evidence_service, "get_store", lambda: store)
    ids = [world.d1.id, world.mid.id, world.old.id]
    save_manifest(
        store,
        CohortManifest(
            cohort_id="ai-test-s1-n3", seed=1, target_size=3, size=3, dataset_version=world.dataset,
            as_of=ON, difficult_share_target=0.0, difficult_count=0, created_at=datetime.now(UTC),
            distributions={}, strata=1, resident_ids_sha256=ids_sha256(ids), resident_ids=ids,
        ),
    )  # fmt: skip
    result = world.run(sample="ai_cohort", cohort_id="ai-test-s1-n3", n=None, dataset_version=None)
    assert result["sample"]["evaluated"] == 3 and result["sample"]["cohort_id"] == "ai-test-s1-n3"
    assert result["diff"]["newly_due"] == 1 and result["estimate"] is None  # stratified: never scaled
    assert any("stratified" in n for n in result["notes"])


# ---------------------------------------------------------------------------------------------- API and job
def submit(client, headers, world, **body):
    gid = str(world.v2.guideline_id)
    return client.post(
        f"/api/v1/guidelines/{gid}/versions/{world.v2.id}/dry-run",
        headers=headers,
        json={"sample": "random_n", "n": 50, "dataset_version": world.dataset, **body},
    )


def run_job(world, job_id):
    job = jobs.get_job(world.s, job_id)
    job.status, job.locked_by = "running", "test-worker"  # claim our own job (the shared DB may hold others)
    world.s.flush()
    handle_dry_run(JobContext(world.h.session_factory(world.s), job, "test-worker"))
    assert jobs.finish_success(world.s, job_id, "test-worker") == "completed"
    world.s.refresh(job)
    return job


def test_the_dry_run_job_stores_its_result_and_the_api_serves_it(world, api_client, auth_headers):
    admin = auth_headers("population_admin")
    resp = submit(api_client, admin, world, district=None)
    assert resp.status_code == 202, resp.text
    data = resp.json()["data"]
    assert data["status"] == "queued" and data["result"] is None and data["version_id"] == str(world.v2.id)
    job_id = uuid.UUID(data["id"])
    row = jobs.get_job(world.s, job_id)
    assert row.job_type == "guidelines.dry_run" and row.max_retries == 0  # no automatic retry
    before = world.row_counts()

    job = run_job(world, job_id)
    assert (job.total_items, job.processed_items, job.succeeded_items, job.failed_items) == (5, 5, 5, 0)
    assert world.row_counts() == before

    gid, vid = world.v2.guideline_id, world.v2.id
    got = api_client.get(f"/api/v1/guidelines/{gid}/versions/{vid}/dry-runs/{job_id}", headers=admin)
    assert got.status_code == 200
    body = got.json()["data"]
    assert body["status"] == "completed" and body["result"]["diff"]["newly_due"] == 2
    assert body["result"]["persisted"] is False and body["completed_at"] is not None
    listed = api_client.get(f"/api/v1/guidelines/{gid}/versions/{vid}/dry-runs", headers=admin).json()["data"]
    assert [d["id"] for d in listed] == [str(job_id)]
    audited = world.s.execute(
        text("SELECT details FROM audit_events WHERE action = 'guideline.dry_run' AND resource_id = :j"),
        {"j": str(world.v2.id)},
    ).all()
    assert audited and audited[0][0]["persisted"] is False and audited[0][0]["newly_due"] == 2


def test_only_authoring_roles_may_run_but_readers_may_read_results(world, api_client, auth_headers):
    gid, vid = world.v2.guideline_id, world.v2.id
    for role, may_run in (
        ("observer", False),
        ("care_coordinator", False),
        ("population_admin", True),
        ("clinical_reviewer", True),
    ):
        resp = submit(api_client, auth_headers(role), world)
        assert resp.status_code == (202 if may_run else 403), (role, resp.text)
        listing = api_client.get(
            f"/api/v1/guidelines/{gid}/versions/{vid}/dry-runs", headers=auth_headers(role)
        )
        assert listing.status_code == 200, role  # results are aggregates and ids: guidelines.view is enough
    assert api_client.get(f"/api/v1/guidelines/{gid}/versions/{vid}/dry-runs").status_code == 401


def test_dry_run_validation_errors(world, api_client, auth_headers):
    admin = auth_headers("population_admin")
    gid = world.v2.guideline_id
    assert submit(api_client, admin, world, n=10_001).status_code == 422  # the cap is 10,000
    assert submit(api_client, admin, world, sample="everyone").status_code == 422
    assert submit(api_client, admin, world, district="D99").status_code == 422  # empty sample
    assert submit(api_client, admin, world, dataset_version="syn-nope").status_code == 404
    other = api_client.post(
        f"/api/v1/guidelines/{uuid.uuid4()}/versions/{world.v2.id}/dry-run", headers=admin, json={}
    )
    assert other.status_code == 404  # version does not belong to that guideline
    missing = api_client.get(
        f"/api/v1/guidelines/{gid}/versions/{world.v2.id}/dry-runs/{uuid.uuid4()}", headers=admin
    )
    assert missing.status_code == 404


def test_a_job_of_another_version_is_not_served_under_this_one(world, api_client, auth_headers, seed):
    admin = auth_headers("population_admin")
    job_id = submit(api_client, admin, world).json()["data"]["id"]
    gid, other = world.v1.guideline_id, world.v1.id  # v1 belongs to the same guideline but is another version
    resp = api_client.get(f"/api/v1/guidelines/{gid}/versions/{other}/dry-runs/{job_id}", headers=admin)
    assert resp.status_code == 404


def test_the_dry_run_module_has_no_write_path():
    """A tripwire next to the behavioural tests above: the module never adds, inserts, deletes or commits."""
    src = open(dryrun.__file__, encoding="utf-8").read()
    assert not any(token in src for token in (".add(", "insert(", "delete(", ".commit(", "update("))
