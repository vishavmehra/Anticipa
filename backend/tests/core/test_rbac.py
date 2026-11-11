"""RBAC map invariants (ADR-060): the observer role is strictly read-only."""

from __future__ import annotations

import pytest

from anticipa.core import rbac


def test_every_role_has_an_explicit_entry():
    assert set(rbac.ROLES) == {"population_admin", "care_coordinator", "clinical_reviewer", "system_admin", "observer"}
    assert set(rbac.ROLE_PERMISSIONS) == set(rbac.ROLES)


def test_observer_holds_exactly_the_plain_view_permissions():
    observer = rbac.permissions_for("observer")
    assert observer == {p for p in rbac.ALL_PERMISSIONS if p.endswith(".view")}
    assert observer == rbac.PLAIN_VIEW_PERMISSIONS
    assert "audit.view" in observer and "integrations.view" in observer


def test_observer_has_no_state_changing_permission():
    state_changing = rbac.ALL_PERMISSIONS - rbac.VIEW_PERMISSIONS
    assert state_changing, "expected at least one state-changing permission to exist"
    assert not rbac.permissions_for("observer") & state_changing
    for name in rbac.permissions_for("observer"):
        assert name.endswith(".view"), name
    for forbidden in ("processing.execute", "caregaps.manage", "caregaps.close", "evidence.review", "system.configure"):
        assert not rbac.has_permission("observer", forbidden)


def test_view_permissions_are_named_as_views():
    for name in rbac.VIEW_PERMISSIONS:
        assert ".view" in name, name
    for name in rbac.ALL_PERMISSIONS - rbac.VIEW_PERMISSIONS:
        assert ".view" not in name, name


@pytest.mark.parametrize("role", ["", "admin", "OBSERVER", "superuser", "service_role"])
def test_unknown_role_gets_nothing(role):
    assert rbac.permissions_for(role) == frozenset()
    assert not rbac.has_permission(role, "jobs.view")


def test_scoped_variants_are_not_widened_for_other_roles():
    # coordinators/reviewers only see their own audit events; only admins see the whole log
    assert rbac.has_permission("care_coordinator", "audit.view_scoped")
    assert not rbac.has_permission("care_coordinator", "audit.view")
    assert not rbac.has_permission("clinical_reviewer", "audit.view")
    assert rbac.has_permission("population_admin", "audit.view")


def test_job_submission_is_limited_to_admin_roles():
    holders = {r for r in rbac.ROLES if rbac.has_permission(r, rbac.PROCESSING_EXECUTE)}
    assert holders == {"population_admin", "system_admin"}
