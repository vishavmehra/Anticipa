"""Care-gap APIs (05 §12): list/get cases, validated status changes, assignment, escalation, reopening, and case
generation from evaluations. Permissions: read = caregaps.view (observer included); status/assignment/escalate/reopen
need caregaps.manage, caregaps.review or caregaps.close (the lifecycle decides which, see lifecycle.required_permissions);
generation jobs need processing.execute. Every mutation is audited and written to the case history."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from anticipa.api.deps import PageDep, SessionDep
from anticipa.api.envelope import Envelope, PagedEnvelope, ok, paged, paginate
from anticipa.api.jobs import JobOut, to_out
from anticipa.caregaps import service
from anticipa.caregaps.lifecycle import ALLOWED
from anticipa.caregaps.models import CASE_STATUSES, PRIORITIES, TERMINAL_STATUSES, CareGapCase, CaseStatusHistory
from anticipa.core import audit
from anticipa.core.auth import CurrentUser, require_any_permission, require_permission
from anticipa.core.rbac import CAREGAPS_CLOSE, CAREGAPS_MANAGE, CAREGAPS_REVIEW, CAREGAPS_VIEW, PROCESSING_EXECUTE
from anticipa.datafabric.models import Resident
from anticipa.guidelines.models import EligibilityEvaluation, Guideline, GuidelineVersion, RuleVersion
from anticipa.jobs import service as jobs_service

router = APIRouter(prefix="/care-gaps", tags=["care-gaps"])
ViewUser = Annotated[CurrentUser, Depends(require_permission(CAREGAPS_VIEW))]
CaseUser = Annotated[CurrentUser, Depends(require_any_permission(CAREGAPS_MANAGE, CAREGAPS_REVIEW, CAREGAPS_CLOSE))]
ManageUser = Annotated[CurrentUser, Depends(require_permission(CAREGAPS_MANAGE))]
ExecuteUser = Annotated[CurrentUser, Depends(require_permission(PROCESSING_EXECUTE))]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StatusIn(_In):
    status: Literal[*CASE_STATUSES]  # type: ignore[valid-type]
    reason: str | None = Field(None, max_length=1000)


class AssignmentIn(_In):
    assigned_coordinator_id: uuid.UUID | None


class ReasonIn(_In):
    reason: str = Field(min_length=3, max_length=1000)


class GenerationJobIn(_In):
    rule_version_ids: list[uuid.UUID] | None = Field(None, max_length=200)
    outcomes: list[Literal["eligible", "requires_clinical_review", "insufficient_evidence"]] = Field(
        default_factory=lambda: ["eligible", "requires_clinical_review"]
    )
    auto_approve: bool = True
    idempotency_key: str | None = Field(None, max_length=200)


class CaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    resident_id: uuid.UUID
    district_code: str | None = None
    evaluation_id: uuid.UUID
    guideline_version_id: uuid.UUID
    rule_version_id: uuid.UUID
    care_activity: str
    status: str
    priority: str
    source_outcome: str
    assigned_coordinator_id: uuid.UUID | None
    planning_run_id: uuid.UUID | None
    opened_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    closure_reason: str | None
    synthetic_status: str


class HistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    from_status: str | None
    to_status: str
    actor_id: uuid.UUID | None
    actor_role: str | None
    reason: str | None
    details: dict[str, Any]
    occurred_at: datetime


class CaseDetailOut(CaseOut):
    resident: dict[str, Any]
    evaluation: dict[str, Any]
    rule: dict[str, Any]
    history: list[HistoryOut]
    allowed_transitions: list[str]


def _case_out(case: CareGapCase, district: str | None) -> CaseOut:
    out = CaseOut.model_validate(case)
    out.district_code = district
    return out


def _detail(session: Session, case: CareGapCase) -> CaseDetailOut:
    resident = session.get(Resident, case.resident_id)
    ev = session.get(EligibilityEvaluation, case.evaluation_id)
    rv = session.get(RuleVersion, case.rule_version_id)
    gv = session.get(GuidelineVersion, case.guideline_version_id)
    g = session.get(Guideline, gv.guideline_id) if gv else None
    history = session.execute(
        select(CaseStatusHistory).where(CaseStatusHistory.case_id == case.id).order_by(CaseStatusHistory.occurred_at, CaseStatusHistory.id)
    ).scalars()
    base = CaseOut.model_validate(case).model_dump()
    base["district_code"] = resident.district_code if resident else None
    return CaseDetailOut(
        **base,
        resident={
            "id": str(case.resident_id), "district_code": resident.district_code, "preferred_language": resident.preferred_language,
            "administrative_sex": resident.administrative_sex, "birth_date": resident.birth_date.isoformat() if resident.birth_date else None,
            "synthetic_status": resident.synthetic_status,
        } if resident else {"id": str(case.resident_id)},
        evaluation={
            "id": str(ev.id), "outcome": ev.outcome, "outcome_class": ev.outcome_class, "evaluation_date": ev.evaluation_date.isoformat(),
            "evaluated_at": ev.evaluated_at.isoformat(), "explanation": ev.explanation, "evidence_references": ev.evidence_references,
            "missing_information": ev.missing_information, "exclusion_reasons": ev.exclusion_reasons, "execution_version": ev.execution_version,
        } if ev else {},
        rule={
            "rule_key": rv.rule_key, "rule_version": rv.version, "name": rv.name, "care_activity": rv.care_activity,
            "content_hash": rv.content_hash, "guideline_key": g.guideline_key if g else None, "guideline_name": g.name if g else None,
            "guideline_version": gv.version if gv else None, "disclaimer": gv.disclaimer if gv else None,
            "source_reference": gv.source_reference if gv else None,
        } if rv else {},
        history=[HistoryOut.model_validate(h) for h in history],
        allowed_transitions=sorted(ALLOWED[case.status]) if case.status not in TERMINAL_STATUSES else ["reopen"],
    )


@router.get("", response_model=PagedEnvelope[CaseOut], summary="List care-gap cases (newest first)")
def list_cases(
    request: Request, session: SessionDep, user: ViewUser, params: PageDep,
    status: Annotated[list[Literal[*CASE_STATUSES]] | None, Query()] = None,  # type: ignore[valid-type]
    priority: Annotated[Literal[*PRIORITIES] | None, Query()] = None,  # type: ignore[valid-type]
    district: Annotated[str | None, Query(max_length=50)] = None,
    care_activity: Annotated[str | None, Query(max_length=100)] = None,
    assigned_coordinator_id: uuid.UUID | None = None, unassigned: bool = False, resident_id: uuid.UUID | None = None,
    opened_from: date | None = None, opened_to: date | None = None,
):  # fmt: skip
    stmt = select(CareGapCase).order_by(CareGapCase.opened_at.desc(), CareGapCase.id.desc())
    if district:
        stmt = stmt.join(Resident, Resident.id == CareGapCase.resident_id).where(Resident.district_code == district)
    if status:
        stmt = stmt.where(CareGapCase.status.in_(status))
    if priority:
        stmt = stmt.where(CareGapCase.priority == priority)
    if care_activity:
        stmt = stmt.where(CareGapCase.care_activity == care_activity)
    if assigned_coordinator_id:
        stmt = stmt.where(CareGapCase.assigned_coordinator_id == assigned_coordinator_id)
    if unassigned:
        stmt = stmt.where(CareGapCase.assigned_coordinator_id.is_(None))
    if resident_id:
        stmt = stmt.where(CareGapCase.resident_id == resident_id)
    if opened_from:
        stmt = stmt.where(CareGapCase.opened_at >= opened_from)
    if opened_to:
        stmt = stmt.where(CareGapCase.opened_at < date.fromordinal(opened_to.toordinal() + 1))
    rows, total = paginate(session, stmt, params)
    districts = dict(session.execute(select(Resident.id, Resident.district_code).where(Resident.id.in_({c.resident_id for c in rows}))).all()) if rows else {}
    return paged(request, [_case_out(c, districts.get(c.resident_id)) for c in rows], params, total)


@router.post("/generation-jobs", response_model=Envelope[JobOut], status_code=202, summary="Create cases from the latest eligibility evaluations (job)")
def submit_generation_job(body: GenerationJobIn, request: Request, response: Response, session: SessionDep, user: ExecuteUser):
    scope: dict[str, Any] = {
        "rule_version_ids": [str(x) for x in body.rule_version_ids or []], "outcomes": list(body.outcomes),
        "auto_approve": body.auto_approve,
    }  # fmt: skip
    key = body.idempotency_key or request.headers.get("Idempotency-Key")
    result = jobs_service.submit_job(session, "caregaps.create_cases", scope, key, user.id)
    if result.created:
        audit.record(session, user.actor, "caregap.generation_job_submit", "processing_job", result.job.id, "success", scope)
    session.commit()
    response.status_code = 202 if result.created else 200
    return ok(request, to_out(result.job, user))


@router.get("/{case_id}", response_model=Envelope[CaseDetailOut], summary="Case with evaluation, rule version and status history")
def get_case(case_id: uuid.UUID, request: Request, session: SessionDep, user: ViewUser):
    return ok(request, _detail(session, service.get_case(session, case_id)))


@router.patch("/{case_id}/status", response_model=Envelope[CaseDetailOut], summary="Change case status (validated transitions only)")
def update_status(case_id: uuid.UUID, body: StatusIn, request: Request, session: SessionDep, user: CaseUser):
    case = service.get_case(session, case_id)
    service.transition(session, case, body.status, actor=user.actor, user=user, reason=body.reason)
    session.commit()
    return ok(request, _detail(session, case))


@router.patch("/{case_id}/assignment", response_model=Envelope[CaseDetailOut], summary="Assign (or release) a case")
def update_assignment(case_id: uuid.UUID, body: AssignmentIn, request: Request, session: SessionDep, user: ManageUser):
    case = service.get_case(session, case_id)
    service.assign(session, case, body.assigned_coordinator_id, actor=user.actor, user=user)
    session.commit()
    return ok(request, _detail(session, case))


@router.post("/{case_id}/escalate", response_model=Envelope[CaseDetailOut], summary="Escalate a case to a clinician or administrator")
def escalate(case_id: uuid.UUID, body: ReasonIn, request: Request, session: SessionDep, user: CaseUser):
    case = service.get_case(session, case_id)
    service.escalate(session, case, actor=user.actor, user=user, reason=body.reason)
    session.commit()
    return ok(request, _detail(session, case))


@router.post("/{case_id}/reopen", response_model=Envelope[CaseDetailOut], summary="Reopen a completed, closed or cancelled case")
def reopen(case_id: uuid.UUID, body: ReasonIn, request: Request, session: SessionDep, user: CaseUser):
    case = service.get_case(session, case_id)
    service.reopen(session, case, actor=user.actor, user=user, reason=body.reason)
    session.commit()
    return ok(request, _detail(session, case))
