"""Care-gap case state machine (04 §12.2-12.3): the single source of truth for which status changes are valid
and who
may make them. Other modules (planning, engagement, closure) call `caregaps.service.transition`, never edit
`status`.

    identified -> pending_review | approved_for_planning
    pending_review -> approved_for_planning            (review decision)
    approved_for_planning <-> awaiting_capacity -> planned          (planner, 05 §13.4)
    planned -> outreach_pending -> outreach_completed -> appointment_scheduled -> awaiting_completion_evidence
    awaiting_completion_evidence -> completed          (ONLY via service.complete_case: verified evidence)
    any open status -> escalated | cancelled ; some -> closed_not_completed
    completed | closed_not_completed | cancelled -> pending_review   (ONLY via reopen)
"""

from __future__ import annotations

from anticipa.caregaps.models import CASE_STATUSES, TERMINAL_STATUSES
from anticipa.core.rbac import CAREGAPS_CLOSE, CAREGAPS_MANAGE, CAREGAPS_REVIEW

ALLOWED: dict[str, frozenset[str]] = {
    "identified": frozenset({"pending_review", "approved_for_planning", "escalated", "cancelled"}),
    "pending_review": frozenset({"approved_for_planning", "escalated", "cancelled", "closed_not_completed"}),
    "approved_for_planning": frozenset(
        {"awaiting_capacity", "planned", "pending_review", "escalated", "cancelled"}
    ),
    "awaiting_capacity": frozenset(
        {"approved_for_planning", "planned", "pending_review", "escalated", "cancelled"}
    ),
    "planned": frozenset(
        {"outreach_pending", "awaiting_capacity", "approved_for_planning", "escalated", "cancelled"}
    ),
    "outreach_pending": frozenset(
        {"outreach_completed", "planned", "escalated", "cancelled", "closed_not_completed"}
    ),
    "outreach_completed": frozenset(
        {"appointment_scheduled", "outreach_pending", "escalated", "cancelled", "closed_not_completed"}
    ),
    "appointment_scheduled": frozenset(
        {
            "awaiting_completion_evidence",
            "outreach_pending",
            "planned",
            "escalated",
            "cancelled",
            "closed_not_completed",
        }
    ),
    "awaiting_completion_evidence": frozenset(
        {"completed", "appointment_scheduled", "escalated", "cancelled", "closed_not_completed"}
    ),
    "escalated": frozenset({"pending_review", "approved_for_planning", "cancelled", "closed_not_completed"}),
    "completed": frozenset(),  # reopen only
    "closed_not_completed": frozenset(),
    "cancelled": frozenset(),
}
assert set(ALLOWED) == set(CASE_STATUSES)
REOPEN_TARGET = "pending_review"
REASON_REQUIRED = frozenset({"closed_not_completed", "cancelled", "escalated"})


def required_permissions(from_status: str, to_status: str) -> frozenset[str]:
    """Any ONE of the returned permissions authorises the change (system actors are not checked)."""
    if to_status in TERMINAL_STATUSES:
        return frozenset({CAREGAPS_CLOSE} | ({CAREGAPS_REVIEW} if from_status == "pending_review" else set()))
    if from_status == "pending_review" or to_status in ("pending_review", "escalated"):
        return frozenset({CAREGAPS_MANAGE, CAREGAPS_REVIEW})
    return frozenset({CAREGAPS_MANAGE})


def is_open(status: str) -> bool:
    return status not in TERMINAL_STATUSES
