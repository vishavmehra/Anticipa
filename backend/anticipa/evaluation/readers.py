"""Read-only result readers: the ONLY evaluation code that touches pipeline tables (Docs/09 §9.4).

Callers: anticipa.evaluation.runner. Everything here is a SELECT; the evaluator never writes labels, pipeline data or
application rows (only its own `evaluation_runs`). Instruction: deliver a working, tested, demonstrable Anticipa MVP
including the evaluation report.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from anticipa.ai.models import ModelInvocation
from anticipa.caregaps.models import CareGapCase, CaseStatusHistory
from anticipa.closure.models import CompletionEvidence
from anticipa.consent.service import unreachable_residents
from anticipa.datafabric.models import Dataset, Document, Resident
from anticipa.engagement.models import Appointment, CommunicationEvent
from anticipa.evaluation.extraction import DocInfo
from anticipa.evaluation.matching import Pred
from anticipa.evaluation.models import EvaluationRun
from anticipa.evidence.models import EvidenceFact, EvidenceSource
from anticipa.guidelines.models import EligibilityEvaluation, Guideline, GuidelineVersion
from anticipa.jobs.models import ProcessingJob
from anticipa.planning.models import (
    CapacitySlot,
    Clinic,
    ClinicService,
    PlannedAllocation,
    PlanningRun,
    UnallocatedCase,
)

CHUNK = 5000
EVALUATED_STATUSES = ("extracted", "extraction_failed")
VERIFIED = ("verified_exact", "verified_normalized")
NOT_FOUND = ("quote_not_found", "source_missing")


def chunks(items: Sequence[Any], n: int = CHUNK) -> Iterator[Sequence[Any]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _uuids(ids: Iterable[str | uuid.UUID]) -> list[uuid.UUID]:
    return [i if isinstance(i, uuid.UUID) else uuid.UUID(str(i)) for i in ids]


def load_cohort_manifest(path: str | Path) -> dict[str, Any]:
    """The AI-cohort manifest (`anticipa.evidence.cohort`): resident ids + the seed that drew them."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not data.get("resident_ids"):
        raise ValueError(f"{path} lists no residents")
    return data


def resident_ids_of_dataset(session: Session, dataset_version: str) -> list[str]:
    ds = select(Dataset.id).where(Dataset.dataset_version == dataset_version).scalar_subquery()
    return [str(r) for r in session.execute(select(Resident.id).where(Resident.dataset_id == ds)).scalars()]


def processed_resident_ids(session: Session, dataset_version: str) -> list[str]:
    """Residents of the dataset with at least one document that went through extraction (succeeded or failed)."""
    ds = select(Dataset.id).where(Dataset.dataset_version == dataset_version).scalar_subquery()
    stmt = (select(Document.resident_id).join(Resident, Resident.id == Document.resident_id)
            .where(Resident.dataset_id == ds, Document.processing_status.in_(EVALUATED_STATUSES)).distinct())  # fmt: skip
    return sorted(str(r) for r in session.execute(stmt).scalars())


def load_documents(session: Session, resident_ids: Sequence[str]) -> dict[str, tuple[DocInfo, str]]:
    """document id -> (DocInfo, content_hash) for every document of the residents (any processing status)."""
    out: dict[str, tuple[DocInfo, str]] = {}
    for part in chunks(_uuids(resident_ids)):
        for r in session.execute(
            select(Document.id, Document.resident_id, Document.language, Document.document_type,
                   Document.processing_status, Document.generation_method, Document.content_hash)
            .where(Document.resident_id.in_(part))
        ):  # fmt: skip
            out[str(r.id)] = (
                DocInfo(str(r.id), str(r.resident_id), r.language or "unknown", r.document_type, r.processing_status,
                        r.generation_method or "unknown"),
                r.content_hash,
            )
    return out


def _disposition(validation_status: str) -> str:
    if validation_status == "accepted":
        return "accepted"
    if validation_status in ("rejected", "failed"):
        return "rejected"
    return "review"  # requires_review, and any not-yet-final state (shown to a reviewer, never accepted)


def _quote_class(status: str) -> str:
    return "verified" if status in VERIFIED else "not_found" if status in NOT_FOUND else "error"


def _age(v: Any) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def load_predictions(session: Session, document_ids: Sequence[str]) -> list[Pred]:
    """Persisted evidence facts joined to their (verified) source span, for the given documents."""
    preds: list[Pred] = []
    for part in chunks(_uuids(document_ids)):
        rows = session.execute(
            select(EvidenceFact, EvidenceSource).join(EvidenceSource, EvidenceSource.evidence_fact_id == EvidenceFact.id)
            .where(EvidenceSource.document_id.in_(part))
        )  # fmt: skip
        for f, s in rows:
            q = _quote_class(s.verification_status)
            v = f.value or {}
            preds.append(Pred(
                fact_id=str(f.id), resident_id=str(f.resident_id), document_id=str(s.document_id), category=f.category,
                fact_type=f.fact_type, concept=v.get("concept"), subject_type=f.subject_type,
                relationship=f.relationship_type, assertion=f.assertion_status, temporal=f.temporal_status,
                disposition=_disposition(f.validation_status), quote_class=q,
                span=(s.source_start_offset, s.source_end_offset) if q == "verified" and s.source_end_offset is not None else None,
                event_date=v.get("event_date"), age_at_onset=_age(v.get("age_at_onset")), certainty=f.extraction_confidence,
                provider=f.model_provider, model_id=f.model_id, prompt_version=f.prompt_version,
                subject_person_id=str(f.subject_person_id) if f.subject_person_id else None,
                location_ambiguous=bool(s.location_ambiguous), invocation_id=str(f.invocation_id) if f.invocation_id else None,
            ))  # fmt: skip
    return preds


def load_invocations(session: Session, task_prefix: str = "evidence.") -> list[dict[str, Any]]:
    """Every extraction-task model call (ids only; never prompts or text). Calls are tied to the extraction job that made
    them (`job_id`) and to the facts they produced (`evidence_facts.invocation_id`); the router stores no document id."""
    rows = session.execute(
        select(ModelInvocation.id, ModelInvocation.job_id, ModelInvocation.task, ModelInvocation.provider, ModelInvocation.model_id,
               ModelInvocation.prompt_version, ModelInvocation.input_tokens, ModelInvocation.output_tokens, ModelInvocation.cost_usd,
               ModelInvocation.latency_ms, ModelInvocation.retry_count, ModelInvocation.status, ModelInvocation.error_category,
               ModelInvocation.fallback_used, ModelInvocation.cache_hit)
        .where(ModelInvocation.task.like(f"{task_prefix}%"))
    )  # fmt: skip
    return [{**dict(r._mapping), "id": str(r.id), "job_id": str(r.job_id) if r.job_id else None,
             "cost_usd": None if r.cost_usd is None else float(r.cost_usd)} for r in rows]


def load_extraction_jobs(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ProcessingJob.id, ProcessingJob.job_type, ProcessingJob.status, ProcessingJob.processed_items, ProcessingJob.failed_items,
               ProcessingJob.started_at, ProcessingJob.completed_at)
        .where(ProcessingJob.job_type.like("evidence.extract%"))
    )  # fmt: skip
    return [{**dict(r._mapping), "id": str(r.id)} for r in rows]


def load_residents(session: Session, resident_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for part in chunks(_uuids(resident_ids)):
        for r in session.execute(
            select(Resident.id, Resident.birth_date, Resident.administrative_sex, Resident.preferred_language,
                   Resident.district_code, Resident.deceased_date).where(Resident.id.in_(part))
        ):  # fmt: skip
            out[str(r.id)] = {"birth_date": r.birth_date, "sex": r.administrative_sex, "language": r.preferred_language,
                              "district": r.district_code, "deceased": r.deceased_date}  # fmt: skip
    return out


def latest_evaluations(session: Session, resident_ids: Sequence[str]) -> list[dict[str, Any]]:
    """The latest persisted evaluation per (resident, guideline version) (evaluations are append-only)."""
    out: list[dict[str, Any]] = []
    for part in chunks(_uuids(resident_ids)):
        stmt = (
            select(EligibilityEvaluation.resident_id, Guideline.guideline_key, EligibilityEvaluation.outcome,
                   EligibilityEvaluation.outcome_class, EligibilityEvaluation.reason_code,
                   EligibilityEvaluation.evaluation_date, EligibilityEvaluation.explanation)
            .join(GuidelineVersion, GuidelineVersion.id == EligibilityEvaluation.guideline_version_id)
            .join(Guideline, Guideline.id == GuidelineVersion.guideline_id)
            .where(EligibilityEvaluation.resident_id.in_(part))
            .ext(distinct_on(EligibilityEvaluation.resident_id, EligibilityEvaluation.guideline_version_id))
            .order_by(EligibilityEvaluation.resident_id, EligibilityEvaluation.guideline_version_id,
                      EligibilityEvaluation.evaluated_at.desc())
        )  # fmt: skip
        for r in session.execute(stmt):
            row = dict(r._mapping)
            crit = (row.pop("explanation") or {}).get("criteria") or []  # keep only the completeness verdict (memory)
            out.append({**row, "resident_id": str(r.resident_id),
                        "explanation_complete": bool(crit) and all(str(c.get("detail") or "").strip() for c in crit)})  # fmt: skip
    return out


def completed_planning_runs(session: Session, *, limit: int | None = None) -> list[dict[str, Any]]:
    stmt = (select(PlanningRun.id, PlanningRun.planning_period_start, PlanningRun.planning_period_end,
                   PlanningRun.config, PlanningRun.solver_parameters, PlanningRun.metrics, PlanningRun.seed,
                   PlanningRun.created_at)
            .where(PlanningRun.status == "completed").order_by(PlanningRun.created_at.desc()))  # fmt: skip
    if limit:
        stmt = stmt.limit(limit)
    return [dict(r._mapping) for r in session.execute(stmt)]


def planning_rows(session: Session, run_id: uuid.UUID) -> dict[str, list[dict[str, Any]]]:
    """Persisted allocations (with the slot/service/clinic they point at), unallocated cases and every clinic's slots."""
    allocs = [
        dict(r._mapping)
        for r in session.execute(
            select(PlannedAllocation.id, PlannedAllocation.care_gap_case_id, PlannedAllocation.resident_id,
                   PlannedAllocation.service_key, PlannedAllocation.district_code, PlannedAllocation.language,
                   PlannedAllocation.allocation_status, PlannedAllocation.proposed_start_time,
                   PlannedAllocation.proposed_end_time, PlannedAllocation.capacity_slot_id,
                   CapacitySlot.start_time.label("slot_start"), CapacitySlot.end_time.label("slot_end"),
                   CapacitySlot.available_capacity, CapacitySlot.slot_status,
                   ClinicService.service_key.label("slot_service_key"), ClinicService.active.label("service_active"),
                   ClinicService.appointment_duration_minutes.label("duration_minutes"),
                   Clinic.operating_status.label("clinic_status"), Clinic.district_code.label("clinic_district"))
            .join(CapacitySlot, CapacitySlot.id == PlannedAllocation.capacity_slot_id)
            .join(ClinicService, ClinicService.id == CapacitySlot.clinic_service_id)
            .join(Clinic, Clinic.id == CapacitySlot.clinic_id)
            .where(PlannedAllocation.planning_run_id == run_id, PlannedAllocation.allocation_status != "superseded")
        )  # fmt: skip
    ]
    stmt = select(PlanningRun.completed_at).where(PlanningRun.id == run_id)
    completed = session.execute(stmt).scalar_one_or_none()
    blocked = unreachable_residents(session, completed)  # consent as it stood when the run finished (ADR-070)
    for a in allocs:
        a["consent_denied"] = a["resident_id"] in blocked
    unalloc = [
        dict(r._mapping)
        for r in session.execute(
            select(UnallocatedCase.care_gap_case_id, UnallocatedCase.resident_id, UnallocatedCase.district_code,
                   UnallocatedCase.language, UnallocatedCase.reason_code, UnallocatedCase.explanation,
                   UnallocatedCase.service_key).where(UnallocatedCase.planning_run_id == run_id)
        )  # fmt: skip
    ]
    total = session.execute(
        select(func.count(PlannedAllocation.id)).where(
            PlannedAllocation.planning_run_id == run_id, PlannedAllocation.allocation_status != "superseded")
    ).scalar_one()  # fmt: skip
    return {"allocations": allocs, "unallocated": unalloc, "allocations_total": total}


def capacity_by_district(session: Session, start: datetime, end: datetime) -> dict[str, dict[str, int]]:
    """Open seats and open-slot minutes per clinic district inside [start, end]."""
    rows = session.execute(
        select(Clinic.district_code, func.sum(CapacitySlot.available_capacity),
               func.sum(CapacitySlot.available_capacity * func.extract("epoch", CapacitySlot.end_time - CapacitySlot.start_time) / 60))
        .join(Clinic, Clinic.id == CapacitySlot.clinic_id)
        .where(CapacitySlot.start_time >= start, CapacitySlot.end_time <= end, CapacitySlot.slot_status == "open")
        .group_by(Clinic.district_code)
    )  # fmt: skip
    return {d: {"seats": int(s or 0), "seat_minutes": int(m or 0)} for d, s, m in rows}


def cases_for_planning(session: Session, case_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    found: set[uuid.UUID] = set()
    for part in chunks(list(case_ids)):
        found |= set(session.execute(select(CareGapCase.id).where(CareGapCase.id.in_(part))).scalars())
    return found


def closure_rows(session: Session, resident_ids: Sequence[str] | None) -> dict[str, Any]:
    """Everything the loop-closure metrics need, as plain dicts/counters."""
    cases: list[dict[str, Any]] = []
    for part in chunks(_uuids(resident_ids)) if resident_ids is not None else [None]:
        stmt = select(CareGapCase.id, CareGapCase.resident_id, CareGapCase.status, CareGapCase.care_activity,
                      CareGapCase.closed_at, CareGapCase.synthetic_status)  # fmt: skip
        if part is not None:
            stmt = stmt.where(CareGapCase.resident_id.in_(part))
        cases += [dict(r._mapping) for r in session.execute(stmt)]
    ids = {c["id"] for c in cases}
    evidence = [dict(r._mapping) for r in session.execute(
        select(CompletionEvidence.care_gap_case_id, CompletionEvidence.activity, CompletionEvidence.verification_status,
               CompletionEvidence.synthetic_status)
    ) if r.care_gap_case_id in ids]  # fmt: skip
    history: dict[uuid.UUID, str] = {}
    for r in session.execute(select(CaseStatusHistory.case_id, CaseStatusHistory.to_status)
                             .order_by(CaseStatusHistory.occurred_at, CaseStatusHistory.id)):  # fmt: skip
        if r.case_id in ids:
            history[r.case_id] = r.to_status
    comm = [dict(r._mapping) for r in session.execute(
        select(CommunicationEvent.care_gap_case_id, CommunicationEvent.synthetic_status,
               CommunicationEvent.language, CommunicationEvent.resident_id)) if r.care_gap_case_id in ids]  # fmt: skip
    appts = [dict(r._mapping) for r in session.execute(
        select(Appointment.care_gap_case_id, Appointment.appointment_status, Appointment.synthetic_status))
        if r.care_gap_case_id in ids]  # fmt: skip
    return {"cases": cases, "evidence": evidence, "history_last": history, "communications": comm, "appointments": appts}


def readiness_counts(session: Session) -> dict[str, int]:
    """Cheap counts a run depends on (population job, planning, outreach, closure, extracted facts)."""
    one = lambda stmt: session.execute(stmt).scalar_one()  # noqa: E731
    return {
        "population_jobs_active": one(select(func.count(ProcessingJob.id)).where(ProcessingJob.job_type == "guidelines.evaluate_population",
                                                                               ProcessingJob.status.in_(("running", "queued", "pending")))),
        "planning_runs_completed": one(select(func.count(PlanningRun.id)).where(PlanningRun.status == "completed")),
        "communication_events": one(select(func.count(CommunicationEvent.id))),
        "closure_rows": one(select(func.count(CompletionEvidence.id))) + one(select(func.count(CareGapCase.id)).where(CareGapCase.status == "completed")),
        "evidence_facts": one(select(func.count(EvidenceFact.id))),
        "care_gap_cases": one(select(func.count(CareGapCase.id))),
        "evaluation_runs_final": one(select(func.count()).select_from(EvaluationRun).where(EvaluationRun.run_type == "test_final")),
    }  # fmt: skip


def structured_counts(session: Session, dataset_version: str | None) -> dict[str, int]:
    stmt = select(func.count(Resident.id))
    if dataset_version:
        stmt = stmt.where(Resident.dataset_id == select(Dataset.id).where(Dataset.dataset_version == dataset_version).scalar_subquery())
    residents = session.execute(stmt).scalar_one()
    evaluated = session.execute(select(func.count(func.distinct(EligibilityEvaluation.resident_id)))).scalar_one()
    return {"residents_loaded": residents, "residents_with_guideline_evaluations": evaluated}
