"""Population-scale evaluation: stream residents in bounded chunks, evaluate approved rules deterministically, persist
append-only evaluations, skip residents whose inputs did not change (08 §15).

`evaluate_population` is independent of the job machinery (`handlers.py` wraps it), so tests and benchmarks call it
directly. Memory is bounded by `chunk_size` residents; each chunk is one short transaction, and the keyset cursor
(`last_resident_id`) makes the run resumable.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, insert, or_, select
from sqlalchemy.orm import Session

from anticipa.core.errors import Conflict, NotFound, ValidationFailed
from anticipa.datafabric.models import ClinicalRecord, Dataset, Resident
from anticipa.evidence.models import EvidenceFact
from anticipa.guidelines.evaluator import (
    ENGINE_VERSION,
    EvidenceItem,
    RecordFact,
    ResidentFacts,
    ResidentInputs,
    RuleDeps,
    evaluate_rule,
    input_hash,
)
from anticipa.guidelines.models import EligibilityEvaluation, GuidelineVersion, RuleVersion
from anticipa.guidelines.rules import RuleDefinition, RuleValidationError, parse_rule_definition

MAX_CHUNK = 5000
SessionFactory = Callable[[], Session]  # a sessionmaker, or `JobContext.session`


@dataclass(frozen=True)
class LoadedRule:
    rule_version_id: uuid.UUID
    guideline_version_id: uuid.UUID
    rule_key: str
    care_activity: str
    definition: RuleDefinition
    content_hash: str
    deps: RuleDeps


@dataclass
class EvalScope:
    evaluation_date: date
    rule_version_ids: list[uuid.UUID]
    dataset_version: str | None = None
    resident_ids: list[uuid.UUID] | None = None
    mode: str = "incremental"  # incremental: skip unchanged inputs; full: always write a new evaluation
    chunk_size: int = 1000


@dataclass
class PopulationStats:
    residents: int = 0
    evaluations_written: int = 0
    skipped_unchanged: int = 0
    residents_with_errors: int = 0
    outcomes: Counter = field(default_factory=Counter)
    seconds: float = 0.0
    last_resident_id: uuid.UUID | None = None

    def as_dict(self) -> dict[str, Any]:
        rate = self.residents / self.seconds if self.seconds else 0.0
        return {
            "residents": self.residents, "evaluations_written": self.evaluations_written,
            "skipped_unchanged": self.skipped_unchanged, "residents_with_errors": self.residents_with_errors,
            "outcomes": dict(sorted(self.outcomes.items())), "seconds": round(self.seconds, 3),
            "residents_per_second": round(rate, 1),
            "last_resident_id": str(self.last_resident_id) if self.last_resident_id else None,
        }  # fmt: skip


# ---------------------------------------------------------------- rule selection (approved + effective only)
def resolve_rules(
    session: Session, evaluation_date: date, *, rule_version_ids: list[uuid.UUID] | None = None,
    guideline_version_ids: list[uuid.UUID] | None = None,
) -> list[LoadedRule]:  # fmt: skip
    """Rules to evaluate on `evaluation_date`. Only approved rule versions of approved, effective guideline versions
    qualify (08 §16.4 #8). Explicit ids that do not qualify are an error, never silently dropped. With no ids, each
    guideline contributes its highest approved version effective on the date."""
    stmt = (
        select(RuleVersion, GuidelineVersion)
        .join(GuidelineVersion, GuidelineVersion.id == RuleVersion.guideline_version_id)
        .where(
            RuleVersion.approval_status == "approved",
            GuidelineVersion.review_status == "approved",
            GuidelineVersion.effective_from <= evaluation_date,
            or_(GuidelineVersion.effective_until.is_(None), GuidelineVersion.effective_until >= evaluation_date),
        )
        .order_by(GuidelineVersion.guideline_id, GuidelineVersion.version.desc(), RuleVersion.rule_key)
    )
    if rule_version_ids:
        stmt = stmt.where(RuleVersion.id.in_(rule_version_ids))
    elif guideline_version_ids:
        stmt = stmt.where(GuidelineVersion.id.in_(guideline_version_ids))
    rows = session.execute(stmt).all()
    if rule_version_ids:
        missing = set(rule_version_ids) - {r.id for r, _ in rows}
        if missing:
            raise Conflict(
                "Rule versions are not approved and effective on the evaluation date: "
                + ", ".join(sorted(str(m) for m in missing))
            )
    elif guideline_version_ids:
        missing = set(guideline_version_ids) - {v.id for _, v in rows}
        if missing:
            raise Conflict(
                "Guideline versions are not approved and effective on the evaluation date: "
                + ", ".join(sorted(str(m) for m in missing))
            )
    else:
        best: dict[uuid.UUID, int] = {}
        for _, v in rows:
            best.setdefault(v.guideline_id, v.version)
        rows = [(r, v) for r, v in rows if v.version == best[v.guideline_id]]
    loaded = []
    for rv, gv in rows:
        try:
            definition = parse_rule_definition(rv.rule_definition)
        except RuleValidationError as exc:
            raise ValidationFailed(f"Rule '{rv.rule_key}' does not validate: {exc}") from exc
        loaded.append(
            LoadedRule(rv.id, gv.id, rv.rule_key, rv.care_activity, definition, rv.content_hash, RuleDeps.of(definition))
        )
    return loaded


# ---------------------------------------------------------------- input loading for one chunk
def _utc_date(ts: datetime | None) -> date | None:
    return ts.astimezone(UTC).date() if ts is not None else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def load_inputs(session: Session, rows: list[Any], rules: list[LoadedRule]) -> dict[uuid.UUID, ResidentInputs]:
    """Normalised inputs for a chunk of resident rows (id, birth_date, deceased_date, administrative_sex).

    Clinical records are prefiltered in SQL to the code sets the rules actually use; evidence facts to `accepted`
    ones in the categories the rules use (unaccepted evidence never reaches the engine)."""
    ids = [r.id for r in rows]
    categories: set[str] = set()
    codes: set[str] = set()
    evidence_categories: set[str] = set()
    for lr in rules:
        categories |= lr.deps.clinical_categories
        codes |= {code for _, code in lr.deps.codes}
        evidence_categories |= lr.deps.evidence_categories
    records: dict[uuid.UUID, list[RecordFact]] = {}
    if categories and codes:
        cr = ClinicalRecord
        for x in session.execute(
            select(cr.id, cr.resident_id, cr.category, cr.code_system, cr.code, cr.normalized_code_system,
                   cr.normalized_code, cr.effective_start, cr.status, cr.source_reference)
            .where(cr.resident_id.in_(ids), cr.category.in_(categories), or_(cr.code.in_(codes), cr.normalized_code.in_(codes)))
        ):  # fmt: skip
            records.setdefault(x.resident_id, []).append(
                RecordFact(x.id, x.category, x.code_system, x.code, x.normalized_code_system, x.normalized_code,
                           _utc_date(x.effective_start), x.status, x.source_reference)  # fmt: skip
            )
    facts: dict[uuid.UUID, list[EvidenceItem]] = {}
    if evidence_categories:
        ef = EvidenceFact
        for x in session.execute(
            select(ef.id, ef.resident_id, ef.category, ef.fact_type, ef.subject_type, ef.relationship_type,
                   ef.assertion_status, ef.temporal_status, ef.value["age_at_onset"].astext.label("onset"))
            .where(ef.resident_id.in_(ids), ef.validation_status == "accepted", ef.category.in_(evidence_categories))
        ):  # fmt: skip
            facts.setdefault(x.resident_id, []).append(
                EvidenceItem(x.id, x.category, x.fact_type, x.subject_type, x.relationship_type, x.assertion_status,
                             x.temporal_status, _int_or_none(x.onset), "accepted")  # fmt: skip
            )
    return {
        r.id: ResidentInputs(
            ResidentFacts(r.id, r.birth_date, r.deceased_date, r.administrative_sex),
            records.get(r.id, ()), facts.get(r.id, ()),
        )
        for r in rows
    }


def latest_hashes(session: Session, resident_ids: list[uuid.UUID], rule_ids: list[uuid.UUID]) -> dict[tuple[uuid.UUID, uuid.UUID], str]:
    ee = EligibilityEvaluation
    rows = session.execute(
        select(ee.resident_id, ee.rule_version_id, ee.input_hash)
        .where(ee.resident_id.in_(resident_ids), ee.rule_version_id.in_(rule_ids))
        .distinct(ee.resident_id, ee.rule_version_id)
        .order_by(ee.resident_id, ee.rule_version_id, ee.evaluated_at.desc(), ee.id.desc())
    )
    return {(r.resident_id, r.rule_version_id): r.input_hash for r in rows}


# ---------------------------------------------------------------- the chunk and the population loop
def evaluate_chunk(
    session: Session, rows: list[Any], rules: list[LoadedRule], on: date, *, mode: str = "incremental",
    job_id: uuid.UUID | None = None,
) -> tuple[list[dict[str, Any]], PopulationStats]:  # fmt: skip
    """Evaluate every rule for every resident row; returns the evaluation rows to insert (not yet persisted)."""
    stats = PopulationStats()
    inputs_by = load_inputs(session, rows, rules)
    latest = latest_hashes(session, [r.id for r in rows], [lr.rule_version_id for lr in rules]) if mode == "incremental" else {}
    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for r in rows:
        inputs = inputs_by[r.id]
        had_error = False
        for lr in rules:
            h = input_hash(lr.deps, lr.content_hash, inputs, on)
            if latest.get((r.id, lr.rule_version_id)) == h:
                stats.skipped_unchanged += 1
                continue
            result = evaluate_rule(lr.definition, inputs, on)
            had_error |= result.outcome == "evaluation_error"
            stats.outcomes[result.outcome] += 1
            out.append({
                "id": uuid.uuid4(), "resident_id": r.id, "guideline_version_id": lr.guideline_version_id,
                "rule_version_id": lr.rule_version_id, "outcome": result.outcome, "outcome_class": result.outcome_class,
                "explanation": result.explanation(lr.rule_key, on), "evidence_references": result.evidence_references,
                "missing_information": result.missing_information, "exclusion_reasons": result.exclusion_reasons,
                "input_hash": h, "evaluation_date": on, "evaluated_at": now, "execution_version": ENGINE_VERSION,
                "job_id": job_id, "synthetic_status": r.synthetic_status,
            })  # fmt: skip
        stats.residents += 1
        stats.residents_with_errors += had_error
    stats.evaluations_written = len(out)
    return out, stats


def _resident_query(scope: EvalScope, dataset_id: uuid.UUID | None, after: uuid.UUID | None, limit: int):
    q = select(Resident.id, Resident.birth_date, Resident.deceased_date, Resident.administrative_sex, Resident.synthetic_status)
    if dataset_id is not None:
        q = q.where(Resident.dataset_id == dataset_id)
    if scope.resident_ids is not None:
        q = q.where(Resident.id.in_(scope.resident_ids))
    if after is not None:
        q = q.where(Resident.id > after)
    return q.order_by(Resident.id).limit(limit)


def count_residents(session: Session, scope: EvalScope) -> int:
    dataset_id = _dataset_id(session, scope)
    q = select(func.count()).select_from(Resident)
    if dataset_id is not None:
        q = q.where(Resident.dataset_id == dataset_id)
    if scope.resident_ids is not None:
        q = q.where(Resident.id.in_(scope.resident_ids))
    return session.execute(q).scalar_one()


def _dataset_id(session: Session, scope: EvalScope) -> uuid.UUID | None:
    if scope.dataset_version is None:
        return None
    ds = session.execute(select(Dataset.id).where(Dataset.dataset_version == scope.dataset_version)).scalar_one_or_none()
    if ds is None:
        raise NotFound(f"Dataset '{scope.dataset_version}' not found.")
    return ds


def evaluate_population(
    sessions: SessionFactory, scope: EvalScope, *, job_id: uuid.UUID | None = None,
    resume_after: uuid.UUID | None = None, on_chunk: Callable[[PopulationStats, PopulationStats], None] | None = None,
) -> PopulationStats:  # fmt: skip
    """Run the whole scope. `on_chunk(chunk_stats, total_stats)` runs after each committed chunk (checkpoint, progress,
    cancellation check); an exception it raises stops the run cleanly between chunks."""
    if not 1 <= scope.chunk_size <= MAX_CHUNK:
        raise ValidationFailed(f"chunk_size must be between 1 and {MAX_CHUNK}.")
    if scope.mode not in ("incremental", "full"):
        raise ValidationFailed("mode must be 'incremental' or 'full'.")
    total = PopulationStats()
    t0 = time.perf_counter()
    with sessions() as s:
        rules = resolve_rules(s, scope.evaluation_date, rule_version_ids=scope.rule_version_ids)
        dataset_id = _dataset_id(s, scope)
    if not rules:
        raise ValidationFailed("No approved rules apply on the evaluation date.")
    after = resume_after
    while True:
        with sessions() as s:
            rows = s.execute(_resident_query(scope, dataset_id, after, scope.chunk_size)).all()
            if not rows:
                break
            values, chunk = evaluate_chunk(s, rows, rules, scope.evaluation_date, mode=scope.mode, job_id=job_id)
            if values:
                s.execute(insert(EligibilityEvaluation), values)
            s.commit()
        after = rows[-1].id
        chunk.last_resident_id = after
        total.residents += chunk.residents
        total.evaluations_written += chunk.evaluations_written
        total.skipped_unchanged += chunk.skipped_unchanged
        total.residents_with_errors += chunk.residents_with_errors
        total.outcomes.update(chunk.outcomes)
        total.last_resident_id = after
        total.seconds = time.perf_counter() - t0
        if on_chunk is not None:
            on_chunk(chunk, total)
        if len(rows) < scope.chunk_size:
            break
    total.seconds = time.perf_counter() - t0
    return total


def extrapolate(rate_residents_per_second: float, rules: int) -> dict[str, Any]:
    """Linear extrapolation of a measured rate to the design populations (clearly an estimate, not a measurement)."""
    if rate_residents_per_second <= 0:
        return {}
    return {
        f"{n:,}_residents_minutes": round(n / rate_residents_per_second / 60, 1) for n in (100_000, 1_000_000)
    } | {"basis": f"measured {rate_residents_per_second:.0f} residents/s with {rules} rule(s); linear extrapolation"}
