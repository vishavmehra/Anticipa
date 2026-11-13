"""Guideline Studio dry run (08 §5): evaluate ONE guideline version (draft, in review, approved) on a bounded
sample of residents and report its impact against the version currently in force, WITHOUT persisting anything.

Nothing here writes to `eligibility_evaluations`, `care_gap_cases` or any production table. It reuses the pure
evaluator (`evaluate_guideline`) and the read only input loader (`load_inputs`), and the sessions it opens are
never committed. The result is a small JSON document that the job handler stores as the job checkpoint
(`checkpoint["result"]`).

The sample is either the fixed AI cohort (stratified, so counts are NOT scaled to the population) or a seeded
random sample of up to 10,000 residents (a uniform draw, so the result also carries a clearly labelled
population estimate with a 95% interval). Measured throughput is about 100 residents/s read only, so 10,000
residents take about two minutes for one guideline pair. No LLM is called; the evaluation is deterministic.
"""

from __future__ import annotations

import math
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.orm import Session

from anticipa.core.errors import NotFound, ValidationFailed
from anticipa.datafabric.models import Dataset, Resident
from anticipa.evidence.cohort import age_band
from anticipa.guidelines.evaluator import ENGINE_VERSION, ResidentInputs, evaluate_guideline
from anticipa.guidelines.models import Guideline, GuidelineVersion
from anticipa.guidelines.population import LoadedGuideline, SessionFactory, load_inputs, load_version

JOB_TYPE = "guidelines.dry_run"
MAX_SAMPLE = 10_000
CHUNK = 500
EXAMPLES = 20
# ponytail: the one registered AI cohort of the pilot; a request may name another (`cohort_id`). The document
# store has no listing call, so a default id is the cheapest honest choice. Add a setting if cohorts multiply.
DEFAULT_COHORT_ID = "ai-syn-20261108-s42-s20261111-n10000"
SAMPLES = ("ai_cohort", "random_n")


@dataclass
class DryRunScope:
    version_id: uuid.UUID
    sample: str  # ai_cohort | random_n
    evaluation_date: date
    n: int | None = None  # ai_cohort: None = the whole cohort; random_n: required
    district: str | None = None  # resident.district_code, e.g. "D01"
    cohort_id: str | None = None
    dataset_version: str | None = None  # random_n only
    seed: int = 1

    def as_input(self) -> dict[str, Any]:
        return {
            "guideline_version_id": str(self.version_id),
            "sample": self.sample,
            "evaluation_date": self.evaluation_date.isoformat(),
            "n": self.n,
            "district": self.district,
            "cohort_id": self.cohort_id,
            "dataset_version": self.dataset_version,
            "seed": self.seed,
        }

    @classmethod
    def from_input(cls, raw: dict[str, Any]) -> DryRunScope:
        try:
            return cls(
                version_id=uuid.UUID(raw["guideline_version_id"]),
                sample=raw["sample"],
                evaluation_date=date.fromisoformat(raw["evaluation_date"]),
                n=int(raw["n"]) if raw.get("n") is not None else None,
                district=raw.get("district") or None,
                cohort_id=raw.get("cohort_id") or None,
                dataset_version=raw.get("dataset_version") or None,
                seed=int(raw.get("seed", 1)),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise ValidationFailed(f"Invalid dry run scope: {exc}") from exc


# ---------------------------------------------------------------- sample
def _cohort_ids(cohort_id: str) -> list[uuid.UUID]:
    from anticipa.evidence import service as evidence_service
    from anticipa.evidence.cohort import load_manifest

    try:
        return load_manifest(evidence_service.get_store(), cohort_id).resident_ids
    except FileNotFoundError:
        raise NotFound(f"AI cohort {cohort_id!r} does not exist.") from None
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def _filters(session: Session, scope: DryRunScope) -> list[Any]:
    out: list[Any] = []
    if scope.sample == "ai_cohort":
        out.append(Resident.id.in_(_cohort_ids(scope.cohort_id or DEFAULT_COHORT_ID)))
    elif scope.dataset_version:
        ds = session.execute(
            select(Dataset.id).where(Dataset.dataset_version == scope.dataset_version)
        ).scalar_one_or_none()
        if ds is None:
            raise NotFound(f"Dataset {scope.dataset_version!r} not found.")
        out.append(Resident.dataset_id == ds)
    if scope.district:
        out.append(Resident.district_code == scope.district)
    return out


def resolve_sample(session: Session, scope: DryRunScope) -> tuple[list[uuid.UUID], int]:
    """(resident ids, size of the population they are drawn from). Seeded and deterministic: the same scope
    gives the same residents, ordered by a hash of (seed, id), so a smaller `n` is a prefix of a larger."""
    where = _filters(session, scope)
    population = session.execute(select(func.count()).select_from(Resident).where(*where)).scalar_one()
    limit = min(scope.n or MAX_SAMPLE, MAX_SAMPLE)
    ids = list(
        session.execute(
            select(Resident.id)
            .where(*where)
            .order_by(func.md5(cast(Resident.id, Text) + f":{scope.seed}"), Resident.id)
            .limit(limit)
        ).scalars()
    )
    return ids, population


def check_scope(session: Session, scope: DryRunScope) -> None:
    """Fail at submission (422/404), not in the worker: a known cohort or dataset and a non empty sample."""
    if scope.sample == "random_n" and not scope.n:
        raise ValidationFailed("n is required for a random sample.")
    where = _filters(session, scope)
    if not session.execute(select(Resident.id).where(*where).limit(1)).first():
        raise ValidationFailed("The sample is empty: no residents match the chosen sample and district.")


# ---------------------------------------------------------------- versions
def baseline_version(session: Session, target: GuidelineVersion, on: date) -> GuidelineVersion | None:
    """The approved, effective version of the same guideline other than the one under test: what a resident is
    judged by today. None when the guideline has no other approved version."""
    return session.execute(
        select(GuidelineVersion)
        .where(
            GuidelineVersion.guideline_id == target.guideline_id,
            GuidelineVersion.id != target.id,
            GuidelineVersion.review_status == "approved",
            GuidelineVersion.effective_from <= on,
            or_(GuidelineVersion.effective_until.is_(None), GuidelineVersion.effective_until >= on),
        )
        .order_by(GuidelineVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def _version_meta(lg: LoadedGuideline, review_status: str) -> dict[str, Any]:
    return {
        "id": str(lg.guideline_version_id),
        "version": lg.version,
        "review_status": review_status,
        "content_hash": lg.content_hash,
        "care_activity": lg.care_activity,
    }


# ---------------------------------------------------------------- aggregation
def _nested(counter: dict[str, Counter]) -> dict[str, dict[str, int]]:
    return {k: dict(sorted(v.items())) for k, v in sorted(counter.items())}


def _estimate(k: int, n: int, population: int) -> dict[str, Any]:
    """Share of a uniform random sample scaled to the population, with a 95% normal interval (clearly an
    estimate)."""
    p = k / n if n else 0.0
    se = math.sqrt(p * (1 - p) / n) if n else 0.0
    return {
        "estimate": round(p * population),
        "low": max(0, round((p - 1.96 * se) * population)),
        "high": min(population, round((p + 1.96 * se) * population)),
    }


def _district_names() -> dict[str, dict[str, str]]:
    try:
        from anticipa.analytics.geography import district_map

        return {d["code"]: {"name": d["district"], "state": d["state"]} for d in district_map()["districts"]}
    except Exception:  # noqa: BLE001 - names are decoration; the codes alone are enough
        return {}


def run_dry_run(
    sessions: SessionFactory,
    scope: DryRunScope,
    *,
    on_start: Callable[[int], None] | None = None,
    on_chunk: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Evaluate the version (and its baseline) over the sample and aggregate. Read only: no session is
    committed. `on_start(total)` runs once the sample is known; `on_chunk(residents)` after each chunk."""
    if scope.sample not in SAMPLES:
        raise ValidationFailed(f"sample must be one of {', '.join(SAMPLES)}.")
    if scope.sample == "random_n" and not scope.n:
        raise ValidationFailed("n is required for a random sample.")
    t0 = time.perf_counter()
    on = scope.evaluation_date
    with sessions() as s:
        version = s.get(GuidelineVersion, scope.version_id)
        if version is None:
            raise NotFound("Guideline version not found.")
        guideline = s.get(Guideline, version.guideline_id)
        target = load_version(s, version, guideline.guideline_key)
        base_row = baseline_version(s, version, on)
        baseline = load_version(s, base_row, guideline.guideline_key) if base_row else None
        target_status = version.review_status
        ids, population = resolve_sample(s, scope)
    if not ids:
        raise ValidationFailed("The sample is empty: no residents match the chosen sample and district.")
    if on_start:
        on_start(len(ids))
    loaded = [target] + ([baseline] if baseline else [])

    outcomes: Counter = Counter()
    base_outcomes: Counter = Counter()
    by_district: dict[str, Counter] = defaultdict(Counter)
    by_band: dict[str, Counter] = defaultdict(Counter)
    by_sex: dict[str, Counter] = defaultdict(Counter)
    reasons: Counter = Counter()  # (outcome, reason_code) under the version under test
    base_reasons: Counter = Counter()
    transitions: Counter = Counter()  # (baseline outcome, outcome) where they differ
    newly_due_by: dict[str, Counter] = {"district": Counter(), "age_band": Counter(), "sex": Counter()}
    due_lost_by_district: Counter = Counter()
    change: Counter = Counter()
    errors = 0
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for start in range(0, len(ids), CHUNK):
        chunk_ids = ids[start : start + CHUNK]
        with sessions() as s:
            rows = s.execute(
                select(
                    Resident.id,
                    Resident.birth_date,
                    Resident.deceased_date,
                    Resident.administrative_sex,
                    Resident.district_code,
                ).where(Resident.id.in_(chunk_ids))
            ).all()
            inputs = load_inputs(s, rows, loaded, on)
        order = {rid: i for i, rid in enumerate(chunk_ids)}
        for r in sorted(rows, key=lambda x: order[x.id]):
            res_inputs: ResidentInputs = inputs[r.id]
            t = evaluate_guideline(
                target.definition, res_inputs, on, target.sources, target.eligible_explanation
            )
            b = (
                evaluate_guideline(
                    baseline.definition, res_inputs, on, baseline.sources, baseline.eligible_explanation
                )
                if baseline
                else None
            )
            district, band, sex = (
                r.district_code or "unknown",
                age_band(r.birth_date, on),
                r.administrative_sex or "unknown",
            )
            errors += t.outcome == "evaluation_error"
            outcomes[t.outcome] += 1
            by_district[district][t.outcome] += 1
            by_band[band][t.outcome] += 1
            by_sex[sex][t.outcome] += 1
            if t.reason_code:
                reasons[(t.outcome, t.reason_code)] += 1
            kind = "eligible" if t.outcome == "eligible" else "other"
            if b is not None:
                base_outcomes[b.outcome] += 1
                if b.reason_code:
                    base_reasons[(b.outcome, b.reason_code)] += 1
                if t.outcome == b.outcome:
                    kind = "unchanged"
                else:
                    transitions[(b.outcome, t.outcome)] += 1
                    if t.outcome == "eligible":
                        kind = "newly_due"
                        newly_due_by["district"][district] += 1
                        newly_due_by["age_band"][band] += 1
                        newly_due_by["sex"][sex] += 1
                    elif b.outcome == "eligible":
                        kind = "no_longer_due"
                        due_lost_by_district[district] += 1
                    else:
                        kind = "changed_other"
                change[kind] += 1
            if len(buckets[kind]) < EXAMPLES:
                buckets[kind].append(
                    {
                        "resident_id": str(r.id),
                        "change": kind,
                        "outcome": t.outcome,
                        "reason_code": t.reason_code,
                        "reason": t.explanation_text,
                        "baseline_outcome": b.outcome if b else None,
                        "baseline_reason_code": b.reason_code if b else None,
                        "district": district,
                        "age_band": band,
                        "sex": sex,
                    }
                )
        if on_chunk:
            on_chunk(len(chunk_ids))

    # examples: the residents the change moves first, then a few that did not move
    order_of_kinds = ("newly_due", "no_longer_due", "changed_other", "eligible", "other", "unchanged")
    caps = {"newly_due": 8, "no_longer_due": 5, "changed_other": 4}
    examples: list[dict[str, Any]] = []
    for kind in order_of_kinds:
        examples += buckets[kind][: caps.get(kind, 4)]
    for kind in order_of_kinds:  # top up to EXAMPLES from what is left
        for e in buckets[kind][caps.get(kind, 4) :]:
            if len(examples) < EXAMPLES:
                examples.append(e)
    examples = examples[:EXAMPLES]

    n_eval = len(ids)
    seconds = time.perf_counter() - t0
    diff = None
    if baseline:
        diff = {
            "eligible_baseline": base_outcomes["eligible"],
            "eligible_version": outcomes["eligible"],
            "newly_due": change["newly_due"],
            "no_longer_due": change["no_longer_due"],
            "changed_other": change["changed_other"],
            "unchanged": change["unchanged"],
            "newly_due_by_district": dict(newly_due_by["district"].most_common()),
            "newly_due_by_age_band": dict(newly_due_by["age_band"].most_common()),
            "newly_due_by_sex": dict(newly_due_by["sex"].most_common()),
            "no_longer_due_by_district": dict(due_lost_by_district.most_common()),
            "transitions": [{"from": f, "to": t, "count": c} for (f, t), c in transitions.most_common(10)],
        }
    estimate = None
    if scope.sample == "random_n" and population:
        estimate = {
            "basis": (
                f"A seeded random sample of {n_eval:,} of {population:,} residents. "
                "Estimate with a 95 percent interval."
            ),
            "population_residents": population,
            "eligible": _estimate(outcomes["eligible"], n_eval, population),
            "newly_due": _estimate(change["newly_due"], n_eval, population) if baseline else None,
            "no_longer_due": _estimate(change["no_longer_due"], n_eval, population) if baseline else None,
        }
    reason_rows = [
        {
            "reason_code": code,
            "outcome": outcome,
            "count": c,
            "baseline_count": base_reasons.get((outcome, code), 0) if baseline else None,
        }
        for (outcome, code), c in reasons.most_common(10)
    ]
    notes = [
        "Nothing was saved. No evaluation, care gap case or outreach was created or changed.",
        "The version under test runs on the same deterministic engine as production. No AI model is called.",
    ]
    if scope.sample == "ai_cohort":
        notes.append(
            "The AI cohort is a stratified sample, so these counts are not scaled to the whole population."
        )
    if baseline is None:
        notes.append(
            "This guideline has no other approved version in force, so there is nothing to compare against."
        )
    return {
        "persisted": False,
        "engine_version": ENGINE_VERSION,
        "guideline": {
            "id": str(version.guideline_id),
            "key": guideline.guideline_key,
            "name": guideline.name,
        },
        "version": _version_meta(target, target_status),
        "baseline": _version_meta(baseline, "approved") if baseline else None,
        "evaluation_date": on.isoformat(),
        "sample": {
            "type": scope.sample,
            "requested": scope.n,
            "evaluated": n_eval,
            "cohort_id": (scope.cohort_id or DEFAULT_COHORT_ID) if scope.sample == "ai_cohort" else None,
            "district": scope.district,
            "seed": scope.seed,
            "population_residents": population,
        },
        "runtime_seconds": round(seconds, 2),
        "residents_per_second": round(n_eval / seconds, 1) if seconds else None,
        "residents_with_errors": errors,
        "outcomes": dict(sorted(outcomes.items())),
        "baseline_outcomes": dict(sorted(base_outcomes.items())) if baseline else None,
        "by_district": _nested(by_district),
        "by_age_band": _nested(by_band),
        "by_sex": _nested(by_sex),
        "district_names": {c: n for c, n in _district_names().items() if c in by_district},
        "diff": diff,
        "top_reason_codes": reason_rows,
        "examples": examples,
        "estimate": estimate,
        "notes": notes,
    }
