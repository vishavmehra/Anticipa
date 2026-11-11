"""Loop closure (05 §15, 02 §15.3): completion evidence and case closure.

Policy (the single place it lives):
  * A case is `completed` ONLY through `close_case(outcome="completed")`, which needs VERIFIED completion evidence for
    that case and a case lifecycle that allows completion (awaiting_completion_evidence). Messages, responses and
    appointments are never evidence.
  * Evidence submitted through the API starts `pending`; a different person (caregaps.review or caregaps.close) verifies
    or rejects it. Only evidence produced by the simulation (a system actor, `simulated_procedure_record`) is verified on
    arrival, and it is labelled simulated.
  * Closing WITHOUT completion is an authorised exception (`closed_not_completed`): needs caregaps.close, an exception
    type and a reason, and is recorded in the case history and audit log.
Status changes go through `caregaps.service` (INVALID_TRANSITION on bad moves). Functions flush, never commit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from anticipa.caregaps import service as caregaps
from anticipa.caregaps.models import TERMINAL_STATUSES, CareGapCase
from anticipa.caregaps.service import (
    _check_scope as check_case_scope,  # one assignment-scope rule for every case action
)
from anticipa.closure.models import EVIDENCE_SOURCES, EXCEPTION_TYPES, CompletionEvidence
from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.core.auth import CurrentUser
from anticipa.core.errors import Conflict, Forbidden, InvalidTransition, NotFound, ValidationFailed
from anticipa.core.rbac import CAREGAPS_CLOSE
from anticipa.datafabric.models import ClinicalRecord, Document


def _now() -> datetime:
    return datetime.now(UTC)


def get_evidence(session: Session, evidence_id: uuid.UUID, *, lock: bool = False) -> CompletionEvidence:
    evidence = session.get(CompletionEvidence, evidence_id, with_for_update=lock, populate_existing=lock)
    if evidence is None:
        raise NotFound("Completion evidence not found.")
    return evidence


def evidence_for_case(session: Session, case_id: uuid.UUID) -> list[CompletionEvidence]:
    return list(session.execute(
        select(CompletionEvidence).where(CompletionEvidence.care_gap_case_id == case_id)
        .order_by(CompletionEvidence.created_at, CompletionEvidence.id)
    ).scalars())  # fmt: skip


def _check_reference(session: Session, case: CareGapCase, source_type: str, source_resource_id: str | None,
                     document_id: uuid.UUID | None) -> None:  # fmt: skip
    """The referenced record/document must exist and belong to the same resident (deterministic; nothing is inferred)."""
    if source_type == "clinical_record":
        try:
            record_id = uuid.UUID(str(source_resource_id))
        except ValueError:
            raise ValidationFailed("A clinical_record reference needs the record id.",
                                   [{"field": "source_resource_id", "reason": "must be a clinical record UUID"}]) from None  # fmt: skip
        owner = session.execute(select(ClinicalRecord.resident_id).where(ClinicalRecord.id == record_id)).scalar_one_or_none()
        if owner != case.resident_id:
            raise ValidationFailed("The clinical record was not found for this resident.",
                                   [{"field": "source_resource_id", "reason": "unknown record for this resident"}])  # fmt: skip
    elif source_type == "document":
        owner = (
            session.execute(select(Document.resident_id).where(Document.id == document_id)).scalar_one_or_none()
            if document_id else None
        )
        if owner != case.resident_id:
            raise ValidationFailed("The document was not found for this resident.",
                                   [{"field": "document_id", "reason": "unknown document for this resident"}])  # fmt: skip


def submit_evidence(
    session: Session, case: CareGapCase, *, source_type: str, evidence_reference: str, activity: str | None = None,
    source_resource_type: str | None = None, source_resource_id: str | None = None, document_id: uuid.UUID | None = None,
    actor: Actor, user: CurrentUser | None = None,
) -> CompletionEvidence:  # fmt: skip
    if source_type not in EVIDENCE_SOURCES:
        raise ValidationFailed("Unknown evidence source.", [{"field": "source_type", "reason": f"one of {list(EVIDENCE_SOURCES)}"}])
    reference = (evidence_reference or "").strip()
    if not reference:
        raise ValidationFailed("An evidence reference is required.", [{"field": "evidence_reference", "reason": "required"}])
    case = caregaps.get_case(session, case.id, lock=True)
    check_case_scope(user, case)
    if case.status in TERMINAL_STATUSES:
        raise Conflict(f"The case is {case.status}; reopen it before adding evidence.")
    if activity is not None and activity != case.care_activity:
        raise ValidationFailed("The evidence activity does not match the case.",
                               [{"field": "activity", "reason": f"case activity is {case.care_activity!r}"}])  # fmt: skip
    _check_reference(session, case, source_type, source_resource_id, document_id)
    auto = source_type == "simulated_procedure_record" and actor.role == "system"
    now = _now()
    evidence = CompletionEvidence(
        id=uuid.uuid4(), care_gap_case_id=case.id, activity=case.care_activity, source_type=source_type,
        source_resource_type=source_resource_type, source_resource_id=source_resource_id, document_id=document_id,
        evidence_reference=reference, verification_status="verified" if auto else "pending",
        verification_method="simulated_record" if auto else None, verified_at=now if auto else None, created_by=actor.id,
        created_at=now, synthetic_status="simulated",
    )  # fmt: skip
    session.add(evidence)
    session.flush()
    audit.record(session, actor, "closure.evidence_submit", "completion_evidence", evidence.id, "success", {
        "care_gap_case_id": str(case.id), "source_type": source_type, "verification_status": evidence.verification_status,
        "simulated": True,
    })  # fmt: skip
    return evidence


def review_evidence(session: Session, evidence: CompletionEvidence, *, decision: str, note: str | None, actor: Actor,
                    user: CurrentUser | None = None) -> CompletionEvidence:  # fmt: skip
    """Verify or reject pending evidence. The reviewer must not be the person who submitted it."""
    if decision not in ("verified", "rejected"):
        raise ValidationFailed("Decision must be verified or rejected.")
    evidence = get_evidence(session, evidence.id, lock=True)
    case = caregaps.get_case(session, evidence.care_gap_case_id, lock=True)
    check_case_scope(user, case)
    if evidence.verification_status != "pending":
        raise InvalidTransition(f"Evidence that is {evidence.verification_status} cannot be reviewed again.")
    if evidence.created_by is not None and evidence.created_by == actor.id:
        raise Forbidden("Evidence must be reviewed by someone other than the person who submitted it.")
    note = (note or "").strip() or None
    if decision == "rejected" and note is None:
        raise ValidationFailed("A note is required when rejecting evidence.", [{"field": "note", "reason": "required"}])
    evidence.verification_status, evidence.reviewed_by, evidence.reviewer_note = decision, actor.id, note
    evidence.verification_method = "reviewer"
    evidence.verified_at = _now() if decision == "verified" else None
    session.flush()
    audit.record(session, actor, "closure.evidence_review", "completion_evidence", evidence.id, "success",
                 {"care_gap_case_id": str(case.id), "decision": decision, "simulated": True})  # fmt: skip
    return evidence


def _verified_evidence(session: Session, case: CareGapCase, evidence_id: uuid.UUID | None) -> CompletionEvidence:
    if evidence_id is not None:
        evidence = get_evidence(session, evidence_id)
        if evidence.care_gap_case_id != case.id:
            raise ValidationFailed("The evidence belongs to a different case.", [{"field": "evidence_id", "reason": "wrong case"}])
        if evidence.verification_status != "verified":
            raise ValidationFailed(f"The evidence is {evidence.verification_status}; verified evidence is required.",
                                   [{"field": "evidence_id", "reason": evidence.verification_status}])  # fmt: skip
        return evidence
    evidence = session.execute(
        select(CompletionEvidence)
        .where(CompletionEvidence.care_gap_case_id == case.id, CompletionEvidence.verification_status == "verified")
        .order_by(CompletionEvidence.verified_at.desc(), CompletionEvidence.id).limit(1)
    ).scalar_one_or_none()  # fmt: skip
    if evidence is None:
        pending = sum(1 for e in evidence_for_case(session, case.id) if e.verification_status == "pending")
        raise ValidationFailed(
            "A case can only be completed with verified completion evidence; an outreach event or appointment is not evidence.",
            [{"field": "evidence_id", "reason": f"no verified evidence ({pending} pending review)"}],
        )  # fmt: skip
    return evidence


def close_case(
    session: Session, case: CareGapCase, *, outcome: str, reason: str, evidence_id: uuid.UUID | None = None,
    exception_type: str | None = None, actor: Actor, user: CurrentUser | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Close a case. `completed` needs verified evidence; `closed_not_completed` is an authorised exception."""
    if outcome not in ("completed", "closed_not_completed"):
        raise ValidationFailed("Outcome must be completed or closed_not_completed.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValidationFailed("A closure reason is required.", [{"field": "reason", "reason": "required"}])
    if user is not None and not user.can(CAREGAPS_CLOSE):
        raise Forbidden()
    case = caregaps.get_case(session, case.id, lock=True)
    check_case_scope(user, case)
    if case.status in TERMINAL_STATUSES:
        raise InvalidTransition(f"A {case.status} case is already closed; reopen it first.")
    evidence: CompletionEvidence | None = None
    if outcome == "completed":
        if exception_type is not None:
            raise ValidationFailed("An exception type only applies to closed_not_completed.",
                                   [{"field": "exception_type", "reason": "not allowed with outcome=completed"}])  # fmt: skip
        evidence = _verified_evidence(session, case, evidence_id)
        caregaps.complete_case(session, case, actor=actor, evidence_reference=f"completion_evidence:{evidence.id}", reason=reason)
    else:
        if exception_type not in EXCEPTION_TYPES:
            raise ValidationFailed("Closing without completion needs an exception type.",
                                   [{"field": "exception_type", "reason": f"one of {list(EXCEPTION_TYPES)}"}])  # fmt: skip
        caregaps.transition(session, case, "closed_not_completed", actor=actor, user=user, reason=f"{exception_type}: {reason}",
                            details={"closure": {"exception_type": exception_type, "authorized_by": str(actor.id) if actor.id else "system"}})  # fmt: skip
    audit.record(session, actor, "closure.close", "care_gap_case", case.id, "success", {
        "outcome": outcome, "evidence_id": str(evidence.id) if evidence else None, "exception_type": exception_type,
        "simulated": True,
    })  # fmt: skip
    return {
        "care_gap_case_id": case.id, "status": case.status, "outcome": outcome, "closed_at": case.closed_at,
        "closure_reason": case.closure_reason, "evidence_id": evidence.id if evidence else None,
        "exception_type": exception_type, "simulated": True, "synthetic_status": "simulated",
    }  # fmt: skip
