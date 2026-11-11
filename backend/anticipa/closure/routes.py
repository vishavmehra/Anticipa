"""Closure APIs (05 §15.4-15.5), mounted under /care-gaps: submit / list / review completion evidence and close a case.

  POST /care-gaps/{id}/completion-evidence                   caregaps.manage or caregaps.close; starts `pending`
  GET  /care-gaps/{id}/completion-evidence                   closure.view (the read-only `observer` role has it)
  POST /care-gaps/{id}/completion-evidence/{eid}/review      caregaps.review or caregaps.close; not the submitter
  POST /care-gaps/{id}/close                                 caregaps.close

`completed` needs VERIFIED evidence; `closed_not_completed` is an authorised exception with a type and a reason.
An outreach event or an appointment alone can never close a case. All records are simulated in the MVP.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from anticipa.api.deps import SessionDep
from anticipa.api.envelope import Envelope, ok
from anticipa.caregaps import service as caregaps
from anticipa.closure import service
from anticipa.closure.models import EVIDENCE_SOURCES, EXCEPTION_TYPES
from anticipa.core.auth import CurrentUser, require_any_permission, require_permission
from anticipa.core.errors import NotFound
from anticipa.core.rbac import CAREGAPS_CLOSE, CAREGAPS_MANAGE, CAREGAPS_REVIEW, CLOSURE_VIEW

router = APIRouter(prefix="/care-gaps", tags=["closure"])
ViewUser = Annotated[CurrentUser, Depends(require_permission(CLOSURE_VIEW))]
SubmitUser = Annotated[CurrentUser, Depends(require_any_permission(CAREGAPS_MANAGE, CAREGAPS_CLOSE))]
ReviewUser = Annotated[CurrentUser, Depends(require_any_permission(CAREGAPS_REVIEW, CAREGAPS_CLOSE))]
CloseUser = Annotated[CurrentUser, Depends(require_permission(CAREGAPS_CLOSE))]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceIn(_In):
    source_type: Literal[*EVIDENCE_SOURCES]  # type: ignore[valid-type]
    evidence_reference: str = Field(min_length=3, max_length=500)
    activity: str | None = Field(None, max_length=100)  # defaults to (and must equal) the case's care activity
    source_resource_type: str | None = Field(None, max_length=64)
    source_resource_id: str | None = Field(None, max_length=200)
    document_id: uuid.UUID | None = None


class ReviewIn(_In):
    decision: Literal["verified", "rejected"]
    note: str | None = Field(None, max_length=1000)


class CloseIn(_In):
    outcome: Literal["completed", "closed_not_completed"]
    reason: str = Field(min_length=3, max_length=1000)
    evidence_id: uuid.UUID | None = None  # default: the latest verified evidence of the case
    exception_type: Literal[*EXCEPTION_TYPES] | None = None  # type: ignore[valid-type]


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    care_gap_case_id: uuid.UUID
    activity: str
    source_type: str
    source_resource_type: str | None
    source_resource_id: str | None
    document_id: uuid.UUID | None
    evidence_reference: str
    verification_status: str
    verification_method: str | None
    reviewed_by: uuid.UUID | None
    reviewer_note: str | None
    verified_at: datetime | None
    created_by: uuid.UUID | None
    created_at: datetime
    synthetic_status: str
    simulated: bool = True


class ClosureOut(BaseModel):
    care_gap_case_id: uuid.UUID
    status: str
    outcome: str
    closed_at: datetime | None
    closure_reason: str | None
    evidence_id: uuid.UUID | None
    exception_type: str | None
    simulated: bool
    synthetic_status: str


@router.post("/{case_id}/completion-evidence", response_model=Envelope[EvidenceOut], status_code=201,
             summary="Submit completion evidence (pending until a different person verifies it)")  # fmt: skip
def submit_evidence(case_id: uuid.UUID, body: EvidenceIn, request: Request, session: SessionDep, user: SubmitUser):
    case = caregaps.get_case(session, case_id)
    evidence = service.submit_evidence(session, case, actor=user.actor, user=user, **body.model_dump())
    session.commit()
    return ok(request, EvidenceOut.model_validate(evidence))


@router.get("/{case_id}/completion-evidence", response_model=Envelope[list[EvidenceOut]],
            summary="Completion evidence of a case")  # fmt: skip
def list_evidence(case_id: uuid.UUID, request: Request, session: SessionDep, user: ViewUser):
    caregaps.get_case(session, case_id)  # 404 for an unknown case
    return ok(request, [EvidenceOut.model_validate(e) for e in service.evidence_for_case(session, case_id)])


@router.post("/{case_id}/completion-evidence/{evidence_id}/review", response_model=Envelope[EvidenceOut],
             summary="Verify or reject pending evidence (not by the person who submitted it)")  # fmt: skip
def review_evidence(case_id: uuid.UUID, evidence_id: uuid.UUID, body: ReviewIn, request: Request, session: SessionDep,
                    user: ReviewUser):  # fmt: skip
    evidence = service.get_evidence(session, evidence_id)
    if evidence.care_gap_case_id != case_id:
        raise NotFound("Completion evidence not found for this case.")
    service.review_evidence(session, evidence, decision=body.decision, note=body.note, actor=user.actor, user=user)
    session.commit()
    return ok(request, EvidenceOut.model_validate(evidence))


@router.post("/{case_id}/close", response_model=Envelope[ClosureOut],
             summary="Close a case: completed (verified evidence) or closed_not_completed (authorised exception)")  # fmt: skip
def close_case(case_id: uuid.UUID, body: CloseIn, request: Request, session: SessionDep, user: CloseUser):
    case = caregaps.get_case(session, case_id)
    result = service.close_case(session, case, actor=user.actor, user=user, **body.model_dump())
    session.commit()
    return ok(request, ClosureOut(**result))
