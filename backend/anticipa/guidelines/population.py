"""Population-scale evaluation: stream residents in bounded chunks, evaluate approved guideline versions
deterministically, persist append-only evaluations, skip residents whose inputs did not change (08 §15).

`evaluate_population` is independent of the job machinery (`handlers.py` wraps it), so tests and benchmarks
call it
directly. Memory is bounded by `chunk_size` residents; each chunk is one short transaction, and the keyset
cursor
(`last_resident_id`) makes the run resumable. One evaluation = one resident x one guideline version.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, insert, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from anticipa.core.errors import Conflict, NotFound, ValidationFailed
from anticipa.datafabric.models import ClinicalRecord, Dataset, Resident
from anticipa.datafabric.profiles import MARKER, PROFILES, RULES
from anticipa.evidence.models import EvidenceFact, EvidenceSource
from anticipa.guidelines.evaluator import (
    ENGINE_VERSION,
    SYSTEM_ALIASES,
    Fact,
    GuidelineDeps,
    ResidentFacts,
    ResidentInputs,
    evaluate_guideline,
    input_hash,
    parse_partial_date,
    record_to_fact,
)
from anticipa.guidelines.models import EligibilityEvaluation, Guideline, GuidelineVersion, RuleVersion
from anticipa.guidelines.rules import GuidelineDefinition, RuleValidationError, parse_guideline_definition

MAX_CHUNK = 5000
URI_BY_SYSTEM = {
    name: uri for uri, name in SYSTEM_ALIASES.items()
}  # selector "SNOMED-CT" -> clinical_records "http://snomed.info/sct"
SessionFactory = Callable[[], Session]  # a sessionmaker, or `JobContext.session`


@dataclass(frozen=True)
class LoadedGuideline:
    guideline_version_id: uuid.UUID
    guideline_id: uuid.UUID
    guideline_key: str
    version: int
    care_activity: str
    content_hash: str
    clinical_review_status: str
    definition: GuidelineDefinition
    sources: list[Any]
    eligible_explanation: str | None
    deps: GuidelineDeps
    rule_versions: dict[str, tuple[uuid.UUID, int]]  # rule_id -> (rule_version id, rule version number)

    def meta(self) -> dict[str, Any]:
        return {
            "id": str(self.guideline_id),
            "key": self.guideline_key,
            "version": self.version,
            "version_id": str(self.guideline_version_id),
            "content_hash": self.content_hash,
        }


@dataclass
class EvalScope:
    evaluation_date: date
    guideline_version_ids: list[uuid.UUID]
    dataset_version: str | None = None
    resident_ids: list[uuid.UUID] | None = None
    mode: str = "incremental"  # incremental: skip unchanged inputs; full: always write a new evaluation
    chunk_size: int = 1000
    storage_profile: str | None = None  # full | rules: only residents stored with that profile (None = all)


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
            "residents": self.residents,
            "evaluations_written": self.evaluations_written,
            "skipped_unchanged": self.skipped_unchanged,
            "residents_with_errors": self.residents_with_errors,
            "outcomes": dict(sorted(self.outcomes.items())),
            "seconds": round(self.seconds, 3),
            "residents_per_second": round(rate, 1),
            "last_resident_id": str(self.last_resident_id) if self.last_resident_id else None,
        }


# ---------------------------------------------------------------- selection (approved + effective only)
def resolve_guidelines(
    session: Session,
    evaluation_date: date,
    *,
    guideline_version_ids: list[uuid.UUID] | None = None,
    rule_version_ids: list[uuid.UUID] | None = None,
    guideline_keys: Sequence[str] | None = None,
) -> list[LoadedGuideline]:
    """Guideline versions to evaluate on `evaluation_date`. Only approved versions effective on the date
    qualify (08 §16.4
    #8). Explicit ids that do not qualify are an error, never silently dropped; `rule_version_ids` select the
    versions that
    contain them (a guideline version is evaluated as a whole). With no ids, each guideline contributes its
    highest
    approved version effective on the date. `guideline_keys` restricts the selection to those guidelines;
    a key with no approved effective version is an error, never silently dropped."""
    wanted = set(guideline_version_ids or [])
    if rule_version_ids:
        found = dict(
            session.execute(
                select(RuleVersion.id, RuleVersion.guideline_version_id).where(
                    RuleVersion.id.in_(rule_version_ids)
                )
            ).all()
        )
        unknown = set(rule_version_ids) - set(found)
        if unknown:
            raise NotFound("Rule versions not found: " + ", ".join(sorted(str(u) for u in unknown)))
        wanted |= set(found.values())
    stmt = (
        select(GuidelineVersion)
        .where(
            GuidelineVersion.review_status == "approved",
            GuidelineVersion.effective_from <= evaluation_date,
            or_(
                GuidelineVersion.effective_until.is_(None),
                GuidelineVersion.effective_until >= evaluation_date,
            ),
        )
        .order_by(GuidelineVersion.guideline_id, GuidelineVersion.version.desc())
    )
    if wanted:
        stmt = stmt.where(GuidelineVersion.id.in_(wanted))
    if guideline_keys:
        stmt = stmt.where(
            GuidelineVersion.guideline_id.in_(
                select(Guideline.id).where(Guideline.guideline_key.in_(list(guideline_keys)))
            )
        )
    versions = list(session.execute(stmt).scalars())
    if wanted:
        missing = wanted - {v.id for v in versions}
        if missing:
            raise Conflict(
                "Guideline versions are not approved and effective on the evaluation date: "
                + ", ".join(sorted(str(m) for m in missing))
            )
    else:
        best: dict[uuid.UUID, int] = {}
        for v in versions:
            best.setdefault(v.guideline_id, v.version)
        versions = [v for v in versions if v.version == best[v.guideline_id]]
    keys = (
        dict(
            session.execute(
                select(Guideline.id, Guideline.guideline_key).where(
                    Guideline.id.in_({v.guideline_id for v in versions})
                )
            ).all()
        )
        if versions
        else {}
    )
    if guideline_keys:
        absent = sorted(set(guideline_keys) - set(keys.values()))
        if absent:
            raise Conflict(
                "Guidelines have no approved version effective on the evaluation date: " + ", ".join(absent)
            )
    return [load_version(session, v, keys[v.guideline_id]) for v in versions]


def load_version(session: Session, v: GuidelineVersion, guideline_key: str) -> LoadedGuideline:
    """The executable form of ONE stored version, whatever its review status. `resolve_guidelines` only passes
    approved versions; the Guideline Studio dry run (dryrun.py) also passes drafts and never persists."""
    rules = list(
        session.execute(
            select(RuleVersion).where(RuleVersion.guideline_version_id == v.id),
            execution_options={"populate_existing": True},
        ).scalars()
    )
    try:
        definition = parse_guideline_definition(
            [r.rule_definition for r in rules], v.selectors, v.decision_policy or None
        )
    except RuleValidationError as exc:
        raise ValidationFailed(
            f"Guideline version {guideline_key} v{v.version} does not validate: {exc}"
        ) from exc
    return LoadedGuideline(
        v.id,
        v.guideline_id,
        guideline_key,
        v.version,
        v.care_activity,
        v.content_hash or "",
        v.clinical_review_status,
        definition,
        list(v.sources),
        (v.version_metadata or {}).get("eligible_explanation"),
        GuidelineDeps.of(definition),
        {r.rule_id: (r.id, r.version) for r in rules},
    )


# ---------------------------------------------------------------- input loading for one chunk
def _utc_date(ts: datetime | None) -> date | None:
    return ts.astimezone(UTC).date() if ts is not None else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def load_inputs(
    session: Session, rows: list[Any], guidelines: list[LoadedGuideline], on: date
) -> dict[uuid.UUID, ResidentInputs]:
    """Normalised inputs for a chunk of resident rows (id, birth_date, deceased_date, administrative_sex).

    Clinical records are prefiltered in SQL to the codes the selectors name; evidence facts to the fact types
    they name
    (rejected/failed ones never load; unaccepted ones load only so the engine marks it unresolved)."""
    ids = [r.id for r in rows]
    codes: set[str] = set()
    systems: set[str] = set()
    fact_types: set[str] = set()
    coverage = False
    for lg in guidelines:
        for _, sel in lg.deps.selectors:
            codes |= {code for _, code in sel.codeset}
            systems |= {URI_BY_SYSTEM.get(system, system) for system, _ in sel.codeset}
            fact_types |= {t for t in sel.fact_type_in if t != "any"}
        coverage |= lg.deps.coverage_years is not None
    facts: dict[uuid.UUID, list[Fact]] = {i: [] for i in ids}
    if codes:
        cr = ClinicalRecord
        # ponytail: matches the ORIGINAL (code_system, code) only. An OR on `normalized_code` (unindexed)
        # turns this into a
        # sequential scan (measured 12 s per 1,000 residents on 25M records vs 1.5 s). Add an index on the
        # normalised
        # columns, then match them here too, once the terminology workstream populates them.
        for x in session.execute(
            select(
                cr.id,
                cr.resident_id,
                cr.category,
                cr.code_system,
                cr.code,
                cr.effective_start,
                cr.effective_end,
                cr.status,
                cr.source_reference,
            ).where(cr.resident_id.in_(ids), cr.code_system.in_(systems), cr.code.in_(codes))
        ):
            fact = record_to_fact(
                x.id,
                x.category,
                x.code_system,
                x.code,
                _utc_date(x.effective_start),
                _utc_date(x.effective_end),
                x.status,
                on,
                x.source_reference,
            )
            if fact is not None:
                facts[x.resident_id].append(fact)
    if fact_types:
        ef, es = EvidenceFact, EvidenceSource
        rows_f = session.execute(
            select(
                ef.id,
                ef.resident_id,
                ef.category,
                ef.fact_type,
                ef.subject_type,
                ef.subject_person_id,
                ef.relationship_type,
                ef.assertion_status,
                ef.temporal_status,
                ef.validation_status,
                ef.value,
            ).where(
                ef.resident_id.in_(ids),
                ef.fact_type.in_(fact_types),
                ef.validation_status.not_in(("rejected", "failed")),
            )
        ).all()
        pointers: dict[uuid.UUID, tuple[uuid.UUID | None, str | None]] = {}
        if rows_f:  # one pointer per fact: its earliest-registered source (document + exact quote)
            for src in session.execute(
                select(es.evidence_fact_id, es.document_id, es.source_quote)
                .where(es.evidence_fact_id.in_([x.id for x in rows_f]))
                .ext(distinct_on(es.evidence_fact_id))
                .order_by(es.evidence_fact_id, es.created_at, es.id)
            ):
                pointers[src.evidence_fact_id] = (src.document_id, src.source_quote)
        for x in rows_f:
            value = x.value if isinstance(x.value, dict) else {}
            rng = parse_partial_date(value.get("event_date"))
            key = str(x.subject_person_id) if x.subject_person_id else (x.relationship_type or "self")
            doc_id, quote = pointers.get(x.id, (None, None))
            facts[x.resident_id].append(
                Fact(
                    str(x.id),
                    "fact",
                    x.category,
                    x.fact_type,
                    None,
                    None,
                    x.subject_type,
                    x.relationship_type,
                    key,
                    x.assertion_status,
                    x.temporal_status,
                    x.validation_status,
                    rng[0] if rng else None,
                    rng[1] if rng else None,
                    _int_or_none(value.get("age_at_onset")),
                    str(doc_id) if doc_id else None,
                    quote,
                )
            )
    latest: dict[uuid.UUID, date | None] = {}
    if coverage:
        cr = ClinicalRecord
        upper = datetime(on.year, on.month, on.day, tzinfo=UTC) + timedelta(days=1)
        for rid, last in session.execute(
            select(cr.resident_id, func.max(cr.effective_start))
            .where(cr.resident_id.in_(ids), cr.effective_start < upper)
            .group_by(cr.resident_id)
        ):
            latest[rid] = _utc_date(last)
    return {
        r.id: ResidentInputs(
            ResidentFacts(r.id, r.birth_date, r.deceased_date, r.administrative_sex),
            facts[r.id],
            latest.get(r.id),
        )
        for r in rows
    }


def latest_hashes(
    session: Session, resident_ids: list[uuid.UUID], guideline_ids: list[uuid.UUID]
) -> dict[tuple[uuid.UUID, uuid.UUID], str]:
    ee = EligibilityEvaluation
    rows = session.execute(
        select(ee.resident_id, ee.guideline_version_id, ee.input_hash)
        .where(ee.resident_id.in_(resident_ids), ee.guideline_version_id.in_(guideline_ids))
        .ext(distinct_on(ee.resident_id, ee.guideline_version_id))
        .order_by(ee.resident_id, ee.guideline_version_id, ee.evaluated_at.desc(), ee.id.desc())
    )
    return {(r.resident_id, r.guideline_version_id): r.input_hash for r in rows}


# ---------------------------------------------------------------- the chunk and the population loop
def evaluate_row(
    lg: LoadedGuideline,
    inputs: ResidentInputs,
    on: date,
    h: str,
    resident_synthetic: str,
    job_id: uuid.UUID | None,
    now: datetime,
) -> dict[str, Any]:
    result = evaluate_guideline(lg.definition, inputs, on, lg.sources, lg.eligible_explanation)
    decisive = lg.rule_versions.get(result.decisive_rule_id or "")
    explanation = result.explanation(lg.meta(), on)
    if decisive:
        explanation["decisive_rule"] = {
            "rule_id": result.decisive_rule_id,
            "rule_version_id": str(decisive[0]),
            "rule_version": decisive[1],
        }
    return {
        "id": uuid.uuid4(),
        "resident_id": inputs.resident.id,
        "guideline_version_id": lg.guideline_version_id,
        "rule_version_id": decisive[0] if decisive else None,
        "outcome": result.outcome,
        "outcome_class": result.outcome_class,
        "reason_code": result.reason_code,
        "review_status": lg.clinical_review_status,
        "explanation": explanation,
        "evidence_references": result.evidence_references,
        "missing_information": result.missing_information,
        "conflicting_information": result.conflicting_information,
        "source_references": result.source_references,
        "exclusion_reasons": result.exclusion_reasons,
        "input_hash": h,
        "evaluation_date": on,
        "evaluated_at": now,
        "execution_version": ENGINE_VERSION,
        "job_id": job_id,
        "synthetic_status": resident_synthetic,
    }


def evaluate_chunk(
    session: Session,
    rows: list[Any],
    guidelines: list[LoadedGuideline],
    on: date,
    *,
    mode: str = "incremental",
    job_id: uuid.UUID | None = None,
) -> tuple[list[dict[str, Any]], PopulationStats]:
    """Evaluate every guideline for every resident row; returns the evaluation rows to insert (not yet
    persisted)."""  # noqa: E501
    stats = PopulationStats()
    inputs_by = load_inputs(session, rows, guidelines, on)
    latest = (
        latest_hashes(session, [r.id for r in rows], [g.guideline_version_id for g in guidelines])
        if mode == "incremental"
        else {}
    )
    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for r in rows:
        inputs = inputs_by[r.id]
        had_error = False
        for lg in guidelines:
            h = input_hash(lg.deps, lg.content_hash, inputs, on)
            if latest.get((r.id, lg.guideline_version_id)) == h:
                stats.skipped_unchanged += 1
                continue
            row = evaluate_row(lg, inputs, on, h, r.synthetic_status, job_id, now)
            had_error |= row["outcome"] == "evaluation_error"
            stats.outcomes[row["outcome"]] += 1
            out.append(row)
        stats.residents += 1
        stats.residents_with_errors += had_error
    stats.evaluations_written = len(out)
    return out, stats


def _profile_clause(profile: str | None):
    """Residents stored with the given profile: lean residents carry `;profile=rules` in `source_version`
    (datafabric.profiles.marked_source_version); everything else, including no marker, is full."""
    if profile is None:
        return None
    if profile not in PROFILES:
        raise ValidationFailed(f"storage_profile must be one of {', '.join(PROFILES)}.")
    lean = func.coalesce(Resident.source_version, "").like(f"%{MARKER}{RULES}%")
    return lean if profile == RULES else ~lean


def _resident_query(scope: EvalScope, dataset_id: uuid.UUID | None, after: uuid.UUID | None, limit: int):
    q = select(
        Resident.id,
        Resident.birth_date,
        Resident.deceased_date,
        Resident.administrative_sex,
        Resident.synthetic_status,
    )
    if dataset_id is not None:
        q = q.where(Resident.dataset_id == dataset_id)
    if scope.resident_ids is not None:
        q = q.where(Resident.id.in_(scope.resident_ids))
    if (clause := _profile_clause(scope.storage_profile)) is not None:
        q = q.where(clause)
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
    if (clause := _profile_clause(scope.storage_profile)) is not None:
        q = q.where(clause)
    return session.execute(q).scalar_one()


def _dataset_id(session: Session, scope: EvalScope) -> uuid.UUID | None:
    if scope.dataset_version is None:
        return None
    ds = session.execute(
        select(Dataset.id).where(Dataset.dataset_version == scope.dataset_version)
    ).scalar_one_or_none()
    if ds is None:
        raise NotFound(f"Dataset '{scope.dataset_version}' not found.")
    return ds


def evaluate_population(
    sessions: SessionFactory,
    scope: EvalScope,
    *,
    job_id: uuid.UUID | None = None,
    resume_after: uuid.UUID | None = None,
    on_chunk: Callable[[PopulationStats, PopulationStats], None] | None = None,
) -> PopulationStats:
    """Run the whole scope. `on_chunk(chunk_stats, total_stats)` runs after each committed chunk (checkpoint,
    progress,
    cancellation check); an exception it raises stops the run cleanly between chunks."""
    if not 1 <= scope.chunk_size <= MAX_CHUNK:
        raise ValidationFailed(f"chunk_size must be between 1 and {MAX_CHUNK}.")
    if scope.mode not in ("incremental", "full"):
        raise ValidationFailed("mode must be 'incremental' or 'full'.")
    _profile_clause(scope.storage_profile)  # validates the value before any work
    total = PopulationStats()
    t0 = time.perf_counter()
    with sessions() as s:
        guidelines = resolve_guidelines(
            s, scope.evaluation_date, guideline_version_ids=scope.guideline_version_ids
        )
        dataset_id = _dataset_id(s, scope)
    if not guidelines:
        raise ValidationFailed("No approved guideline version applies on the evaluation date.")
    after = resume_after
    while True:
        with sessions() as s:
            rows = s.execute(_resident_query(scope, dataset_id, after, scope.chunk_size)).all()
            if not rows:
                break
            values, chunk = evaluate_chunk(
                s, rows, guidelines, scope.evaluation_date, mode=scope.mode, job_id=job_id
            )
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


def extrapolate(rate_residents_per_second: float, guidelines: int) -> dict[str, Any]:
    """Linear extrapolation of a measured rate to the design populations (clearly an estimate, not a
    measurement)."""  # noqa: E501
    if rate_residents_per_second <= 0:
        return {}
    return {
        f"{n:,}_residents_minutes": round(n / rate_residents_per_second / 60, 1) for n in (100_000, 1_000_000)
    } | {
        "basis": f"measured {rate_residents_per_second:.0f} residents/s with {guidelines} guideline version(s); linear extrapolation"  # noqa: E501
    }
