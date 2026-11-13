"""RBAC map invariants (ADR-060): the observer role is strictly read-only."""

from __future__ import annotations

import pytest

from anticipa.core import rbac


def test_every_role_has_an_explicit_entry():
    assert set(rbac.ROLES) == {
        "population_admin",
        "care_coordinator",
        "clinical_reviewer",
        "system_admin",
        "observer",
    }
    assert set(rbac.ROLE_PERMISSIONS) == set(rbac.ROLES)


INTERNAL = {
    "jobs.view",
    "integrations.view",
    "audit.view",
    "audit.view_scoped",
    "audit.view_redacted",
    "system.configure",
}


def test_observer_holds_product_views_only_never_internal_backend_data():
    observer = rbac.permissions_for("observer")
    product_views = {p for p in rbac.ALL_PERMISSIONS if p.endswith(".view")} - INTERNAL
    assert observer == product_views
    assert not observer & INTERNAL  # no jobs, integrations or audit trail (owner decision 2026-11-12)
    assert {
        "analytics.view",
        "residents.view",
        "evidence.view",
        "engagement.view",
        "closure.view",
    } <= observer


def test_internal_backend_data_is_for_system_admin_and_audit_stays_with_its_owners():
    def holders(permission):
        return {r for r in rbac.ROLES if rbac.has_permission(r, permission)}

    assert holders("jobs.view") == {"system_admin"}
    assert holders("integrations.view") == {"system_admin"}
    assert holders("system.configure") == {"system_admin"}  # also gates GET /analytics/processing
    assert holders("audit.view_redacted") == set()  # defined, held by nobody
    assert holders("audit.view") == {"population_admin", "system_admin"}
    assert holders("audit.view_scoped") == {"care_coordinator", "clinical_reviewer"}


def test_observer_has_no_state_changing_permission():
    state_changing = rbac.ALL_PERMISSIONS - rbac.VIEW_PERMISSIONS
    assert state_changing, "expected at least one state-changing permission to exist"
    assert not rbac.permissions_for("observer") & state_changing
    for name in rbac.permissions_for("observer"):
        assert name.endswith(".view"), name
    for forbidden in (
        "processing.execute",
        "caregaps.manage",
        "caregaps.close",
        "evidence.review",
        "system.configure",
    ):
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
