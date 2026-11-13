"""Care-gap state machine (04 §12.2-12.3): the transition table is the contract other modules rely on
(planner, engagement,
closure). Pure unit tests."""

from __future__ import annotations

from collections import deque

from anticipa.caregaps.lifecycle import ALLOWED, REOPEN_TARGET, required_permissions
from anticipa.caregaps.models import CASE_STATUSES, OPEN_STATUSES, TERMINAL_STATUSES
from anticipa.core.rbac import CAREGAPS_CLOSE, CAREGAPS_MANAGE, CAREGAPS_REVIEW


def reachable(start: str) -> set[str]:
    seen, queue = {start}, deque([start])
    while queue:
        for nxt in ALLOWED[queue.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def test_every_status_is_covered_and_terminal_states_have_no_outgoing_transitions():
    assert set(ALLOWED) == set(CASE_STATUSES) and len(CASE_STATUSES) == 13
    assert all(not ALLOWED[s] for s in TERMINAL_STATUSES)  # leaving a terminal state is `reopen` only
    assert REOPEN_TARGET == "pending_review"


def test_every_open_status_can_be_escalated_or_cancelled_and_nothing_goes_back_to_identified():
    for status in OPEN_STATUSES:
        if status != "escalated":
            assert "escalated" in ALLOWED[status], status
        assert "cancelled" in ALLOWED[status], status
        assert "identified" not in ALLOWED[status], status


def test_the_planner_facing_statuses_and_the_happy_path_exist():
    assert (
        "awaiting_capacity" in ALLOWED["approved_for_planning"] and "planned" in ALLOWED["awaiting_capacity"]
    )
    path = [
        "identified",
        "approved_for_planning",
        "awaiting_capacity",
        "planned",
        "outreach_pending",
        "outreach_completed",
        "appointment_scheduled",
        "awaiting_completion_evidence",
        "completed",
    ]
    for a, b in zip(path, path[1:], strict=False):
        assert b in ALLOWED[a], (a, b)
    assert reachable("identified") >= set(OPEN_STATUSES) | {"completed", "closed_not_completed", "cancelled"}


def test_completion_is_reachable_only_from_awaiting_completion_evidence():
    assert [s for s, nxt in ALLOWED.items() if "completed" in nxt] == ["awaiting_completion_evidence"]
    # no shortcut: neither a booking nor an outreach record can complete a case
    for s in ("planned", "outreach_pending", "outreach_completed", "appointment_scheduled"):
        assert "completed" not in ALLOWED[s], s


def test_permissions_required_per_transition():
    assert required_permissions("identified", "approved_for_planning") == {CAREGAPS_MANAGE}
    assert required_permissions("pending_review", "approved_for_planning") == {
        CAREGAPS_MANAGE,
        CAREGAPS_REVIEW,
    }
    assert required_permissions("approved_for_planning", "escalated") == {CAREGAPS_MANAGE, CAREGAPS_REVIEW}
    assert required_permissions("planned", "cancelled") == {CAREGAPS_CLOSE}
    assert required_permissions("pending_review", "closed_not_completed") == {CAREGAPS_CLOSE, CAREGAPS_REVIEW}
    assert required_permissions("cancelled", "pending_review") == {CAREGAPS_MANAGE, CAREGAPS_REVIEW}  # reopen
    assert required_permissions("awaiting_completion_evidence", "completed") == {CAREGAPS_CLOSE}
