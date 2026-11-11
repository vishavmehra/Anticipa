"""Care-gap engine: case creation from eligibility evaluations (04 §12.4), validated lifecycle transitions, assignment,
escalation, reopening. Every state change writes a history row and an audit event. Functions add to the caller's
transaction (flush, no commit) except `create_cases_scan`, which commits per chunk.

`user` (a CurrentUser) is passed by API routes so permissions and assignment scope are enforced; internal callers
(planner, engagement, closure, jobs) pass only an `Actor` and are trusted to have been authorised upstream.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, exists, insert, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from anticipa.caregaps.lifecycle import ALLOWED, REASON_REQUIRED, REOPEN_TARGET, is_open, required_permissions
from anticipa.caregaps.models import TERMINAL_STATUSES, CareGapCase, CaseStatusHistory
from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.core.auth import CurrentUser
from anticipa.core.errors import Conflict, Forbidden, InvalidTransition, NotFound, ValidationFailed
from anticipa.core.rbac import Role
from anticipa.guidelines.models import EligibilityEvaluation, RuleVersion

# evaluation outcome -> initial case status (08 §12: eligible is a candidate; indeterminate goes to review, not planning)
CASE_OUTCOMES = ("eligible", "requires_clinical_review")
SCAN_CHUNK = 1000


def _now() -> datetime:
    return datetime.now(UTC)


def get_case(session: Session, case_id: uuid.UUID, *, lock: bool = False) -> CareGapCase:
    case = session.get(CareGapCase, case_id, with_for_update=lock, populate_existing=lock)
    if case is None:
        raise NotFound("Care-gap case not found.")
    return case


def _check_scope(user: CurrentUser | None, case: CareGapCase) -> None:
    """Coordinators act within their assigned scope: unassigned cases (to claim) or their own (05 §4.3)."""
    if user is not None and user.role == Role.CARE_COORDINATOR and case.assigned_coordinator_id not in (None, user.id):
        raise Forbidden("This case is assigned to another coordinator.")


def _check_permission(user: CurrentUser | None, needed: frozenset[str]) -> None:
    if user is not None and not any(user.can(p) for p in needed):
        raise Forbidden()


def _history(case_id: uuid.UUID, from_status: str | None, to_status: str, actor: Actor, reason: str | None,
             details: dict[str, Any] | None, synthetic_status: str) -> dict[str, Any]:  # fmt: skip
    return {
        "id": uuid.uuid4(), "case_id": case_id, "from_status": from_status, "to_status": to_status,
        "actor_id": actor.id, "actor_role": actor.role, "reason": reason, "details": details or {},
        "occurred_at": _now(), "synthetic_status": synthetic_status,
    }  # fmt: skip


def transition(
    session: Session, case: CareGapCase, to_status: str, *, actor: Actor, user: CurrentUser | None = None,
    reason: str | None = None, details: dict[str, Any] | None = None, _allow_completion: bool = False,
    _reopen: bool = False,
) -> CareGapCase:  # fmt: skip
    """Move a case to `to_status` if the lifecycle allows it. Raises INVALID_TRANSITION otherwise."""
    case = get_case(session, case.id, lock=True)
    frm = case.status
    reason = (reason or "").strip() or None
    if to_status == "completed" and not _allow_completion:
        raise InvalidTransition("A case can only be completed with verified completion evidence (closure workflow).")
    if _reopen:
        valid = frozenset({REOPEN_TARGET}) if frm in TERMINAL_STATUSES else frozenset()
    else:
        valid = ALLOWED[frm]
    if to_status not in valid:
        raise InvalidTransition(
            f"A {frm} case cannot become {to_status}.",
            [{"field": "status", "reason": f"allowed from {frm}: {sorted(valid) or 'none (reopen instead)'}"}],
        )
    if to_status in REASON_REQUIRED and reason is None:
        raise ValidationFailed("A reason is required for this status change.", [{"field": "reason", "reason": "required"}])
    _check_permission(user, required_permissions(frm, to_status))
    _check_scope(user, case)
    if _reopen:
        dup = session.execute(
            select(CareGapCase.id).where(
                CareGapCase.resident_id == case.resident_id, CareGapCase.care_activity == case.care_activity,
                CareGapCase.id != case.id, CareGapCase.status.not_in(TERMINAL_STATUSES),
            )
        ).first()
        if dup:
            raise Conflict("Another open case already exists for this resident and care activity.")
    now = _now()
    case.status = to_status
    case.updated_at = now
    if to_status in TERMINAL_STATUSES:
        case.closed_at, case.closure_reason = now, reason
    else:
        case.closed_at = case.closure_reason = None
    extra = dict(details or {})
    if to_status == "escalated":
        extra["escalated_from"] = frm
    if _reopen:
        extra["reopened_from"] = frm
    try:
        session.flush()
    except IntegrityError as exc:  # lost a race with another open case for the same resident + activity
        raise Conflict("Another open case already exists for this resident and care activity.") from exc
    session.add(CaseStatusHistory(**_history(case.id, frm, to_status, actor, reason, extra, case.synthetic_status)))
    audit.record(session, actor, "caregap.status_change", "care_gap_case", case.id, "success",
                 {"from": frm, "to": to_status, "reason": reason, **extra})  # fmt: skip
    session.flush()
    return case


def escalate(session: Session, case: CareGapCase, *, actor: Actor, user: CurrentUser | None, reason: str) -> CareGapCase:
    return transition(session, case, "escalated", actor=actor, user=user, reason=reason)


def reopen(session: Session, case: CareGapCase, *, actor: Actor, user: CurrentUser | None, reason: str) -> CareGapCase:
    if not (reason or "").strip():
        raise ValidationFailed("A reason is required to reopen a case.", [{"field": "reason", "reason": "required"}])
    if case.status not in TERMINAL_STATUSES:
        raise InvalidTransition(f"A {case.status} case is already open.")
    return transition(session, case, REOPEN_TARGET, actor=actor, user=user, reason=reason, _reopen=True)


def complete_case(
    session: Session, case: CareGapCase, *, actor: Actor, evidence_reference: str, reason: str | None = None
) -> CareGapCase:  # fmt: skip
    """For the closure workflow only: completion needs verified completion evidence, referenced in history/audit."""
    if not (evidence_reference or "").strip():
        raise ValidationFailed("Completion requires a verified completion-evidence reference.")
    return transition(
        session, case, "completed", actor=actor, reason=reason or "Completion evidence verified",
        details={"completion_evidence": evidence_reference}, _allow_completion=True,
    )  # fmt: skip


def assign(
    session: Session, case: CareGapCase, assignee: uuid.UUID | None, *, actor: Actor, user: CurrentUser | None
) -> CareGapCase:  # fmt: skip
    case = get_case(session, case.id, lock=True)
    if not is_open(case.status):
        raise InvalidTransition(f"A {case.status} case cannot be reassigned; reopen it first.")
    _check_scope(user, case)
    if user is not None and user.role == Role.CARE_COORDINATOR and assignee not in (None, user.id):
        raise Forbidden("Coordinators can only assign cases to themselves or release them.")
    previous = case.assigned_coordinator_id
    case.assigned_coordinator_id = assignee
    case.updated_at = _now()
    session.flush()
    audit.record(session, actor, "caregap.assign", "care_gap_case", case.id, "success",
                 {"previous": str(previous) if previous else None, "assigned_to": str(assignee) if assignee else None})  # fmt: skip
    return case


# ---------------------------------------------------------------- case creation (04 §12.4)
def initial_status(outcome: str, auto_approve: bool) -> str:
    if outcome == "eligible":
        return "approved_for_planning" if auto_approve else "identified"
    return "pending_review"  # requires_clinical_review / insufficient_evidence: a person decides, never the planner


def create_cases_from_rows(
    session: Session, candidates: Sequence[Any], *, auto_approve: bool, actor: Actor,
) -> dict[str, int]:  # fmt: skip
    """Insert cases for evaluation rows (id, resident_id, guideline_version_id, rule_version_id, outcome,
    synthetic_status, care_activity, priority). Duplicates (an open case for the same resident + care activity, or a
    case already made from the evaluation) are skipped by the database; history rows are written for the new cases."""
    values = [
        {
            "id": uuid.uuid4(), "resident_id": c.resident_id, "evaluation_id": c.id,
            "guideline_version_id": c.guideline_version_id, "rule_version_id": c.rule_version_id,
            "care_activity": c.care_activity, "status": initial_status(c.outcome, auto_approve), "priority": c.priority,
            "source_outcome": c.outcome, "synthetic_status": c.synthetic_status,
        }
        for c in candidates
    ]  # fmt: skip
    if not values:
        return {"created": 0, "skipped_existing": 0}
    inserted = session.execute(
        pg_insert(CareGapCase).values(values).on_conflict_do_nothing().returning(CareGapCase.id, CareGapCase.evaluation_id)
    ).all()
    by_eval = {v["evaluation_id"]: v for v in values}
    history: list[dict[str, Any]] = []
    for row in inserted:
        v = by_eval[row.evaluation_id]
        base, syn, outcome = {"evaluation_id": str(row.evaluation_id)}, v["synthetic_status"], v["source_outcome"]
        history.append(_history(row.id, None, "identified", actor, f"Case created from eligibility evaluation ({outcome})", base, syn))
        if v["status"] != "identified":
            why = (
                "Auto-approved: deterministic eligibility under an approved rule version"
                if v["status"] == "approved_for_planning"
                else f"Needs clinical review before planning (evaluation outcome: {outcome})"
            )
            history.append(_history(row.id, "identified", v["status"], actor, why, base, syn))
    if history:
        session.execute(insert(CaseStatusHistory), history)
    return {"created": len(inserted), "skipped_existing": len(values) - len(inserted)}


def create_cases_scan(
    sessions: Callable[[], Session], rule_version_ids: Sequence[uuid.UUID] | None = None, *,
    outcomes: Sequence[str] = CASE_OUTCOMES, auto_approve: bool = True, actor: Actor | None = None,
    chunk_size: int = SCAN_CHUNK, on_chunk: Callable[[dict[str, int]], None] | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Create cases for every LATEST evaluation (per resident + rule version) with a case-worthy outcome that has no
    case yet. Idempotent and resumable by construction. Returns counts; commits per chunk."""
    actor = actor or Actor.system()
    bad = [o for o in outcomes if o not in ("eligible", "requires_clinical_review", "insufficient_evidence")]
    if bad:
        raise ValidationFailed(f"Outcomes {bad} never create cases.")
    ee, newer, rv = EligibilityEvaluation, aliased(EligibilityEvaluation), RuleVersion
    totals = {"created": 0, "skipped_existing": 0, "scanned": 0}
    last: uuid.UUID | None = None
    while True:
        with sessions() as s:
            stmt = (
                select(ee.id, ee.resident_id, ee.guideline_version_id, ee.rule_version_id, ee.outcome, ee.synthetic_status,
                       rv.care_activity, rv.priority)
                .join(rv, rv.id == ee.rule_version_id)
                .where(
                    ee.outcome.in_(list(outcomes)),
                    ~exists().where(
                        newer.resident_id == ee.resident_id, newer.rule_version_id == ee.rule_version_id,
                        or_(newer.evaluated_at > ee.evaluated_at, and_(newer.evaluated_at == ee.evaluated_at, newer.id > ee.id)),
                    ),
                    ~exists().where(CareGapCase.evaluation_id == ee.id),
                )
                .order_by(ee.id)
                .limit(chunk_size)
            )  # fmt: skip
            if rule_version_ids:
                stmt = stmt.where(ee.rule_version_id.in_(list(rule_version_ids)))
            if last is not None:
                stmt = stmt.where(ee.id > last)
            rows = s.execute(stmt).all()
            if not rows:
                break
            counts = create_cases_from_rows(s, rows, auto_approve=auto_approve, actor=actor)
            s.commit()
        last = rows[-1].id
        totals["scanned"] += len(rows)
        totals["created"] += counts["created"]
        totals["skipped_existing"] += counts["skipped_existing"]
        if on_chunk is not None:
            on_chunk({"scanned": len(rows), **counts})
        if len(rows) < chunk_size:
            break
    return totals
