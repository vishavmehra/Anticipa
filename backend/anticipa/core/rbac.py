"""Explicit role -> permission map (05 §4.3, ADR-060). Deny by default; scope limits ("assigned",
"review") are enforced by the owning module on top of these permissions. Later waves extend
ROLE_PERMISSIONS here, never with ad-hoc role checks.

Naming makes read-only explicit: `<module>.view` (and `*.view_limited/_scoped/_redacted`) only read;
anything that changes state, reviews, approves, closes, configures or triggers processing/AI uses
`.manage/.review/.approve/.close/.execute/.configure`. The `observer` role (hackathon judges, ADR-060)
holds only view-style permissions.

Internal backend data (jobs, integration health, processing and AI spend) is for operators only (owner
decision 2026-11-12): `jobs.view` and `integrations.view` belong to system_admin alone, and `system.configure`
(also system_admin only) gates GET /analytics/processing. The audit trail stays with population_admin (full)
and coordinators / reviewers (own actions only). `audit.view_redacted` is defined but no role holds it.

Mapping of the 05 §4.3 qualifiers: Yes/Authorized -> full permission; Limited/scoped -> the
`*_limited`/`*_scoped` variant; "No by default" -> not granted (grant explicitly if the owner decides).
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    POPULATION_ADMIN = "population_admin"
    CARE_COORDINATOR = "care_coordinator"
    CLINICAL_REVIEWER = "clinical_reviewer"
    SYSTEM_ADMIN = "system_admin"
    OBSERVER = "observer"


ROLES: tuple[str, ...] = tuple(r.value for r in Role)

# --- read-only permissions
# -------------------------------------------------------------------------------------
ANALYTICS_VIEW = "analytics.view"
ANALYTICS_VIEW_LIMITED = "analytics.view_limited"
RESIDENTS_VIEW = "residents.view"  # modules narrow to assigned/review scope for those roles
RECORDS_VIEW = "records.view"
DOCUMENTS_VIEW = "documents.view"
EVIDENCE_VIEW = "evidence.view"
GUIDELINES_VIEW = "guidelines.view"
RULES_VIEW = "rules.view"  # rules, eligibility evaluations
CAREGAPS_VIEW = "caregaps.view"
CAPACITY_VIEW = "capacity.view"  # clinic capacity and planning results
ENGAGEMENT_VIEW = "engagement.view"  # outreach/communications
APPOINTMENTS_VIEW = "appointments.view"
CLOSURE_VIEW = "closure.view"
CONSENT_VIEW = "consent.view"  # a resident's outreach / AI-processing consent decisions (ADR-070)
JOBS_VIEW = "jobs.view"
INTEGRATIONS_VIEW = "integrations.view"
AUDIT_VIEW = "audit.view"
AUDIT_VIEW_SCOPED = "audit.view_scoped"  # own actions only
AUDIT_VIEW_REDACTED = "audit.view_redacted"  # all events, without actor ids / request ids / details

# --- state-changing permissions
# --------------------------------------------------------------------------------
EVIDENCE_REVIEW = "evidence.review"
GUIDELINES_APPROVE = "guidelines.approve"
PROCESSING_EXECUTE = (
    "processing.execute"  # submit / retry / cancel jobs; anything that triggers processing or AI
)
CAPACITY_MANAGE = "capacity.manage"
CAREGAPS_MANAGE = "caregaps.manage"
CAREGAPS_CLOSE = "caregaps.close"
CAREGAPS_REVIEW = "caregaps.review"
CONSENT_MANAGE = "consent.manage"  # record a resident's permit / deny decision
SYSTEM_CONFIGURE = "system.configure"

VIEW_PERMISSIONS: frozenset[str] = frozenset({
    ANALYTICS_VIEW, ANALYTICS_VIEW_LIMITED, RESIDENTS_VIEW, RECORDS_VIEW, DOCUMENTS_VIEW, EVIDENCE_VIEW,
    GUIDELINES_VIEW, RULES_VIEW, CAREGAPS_VIEW, CAPACITY_VIEW, ENGAGEMENT_VIEW, APPOINTMENTS_VIEW,
    CLOSURE_VIEW, CONSENT_VIEW, JOBS_VIEW, INTEGRATIONS_VIEW, AUDIT_VIEW, AUDIT_VIEW_SCOPED,
    AUDIT_VIEW_REDACTED,
})  # fmt: skip

# What product users and judges may read: no jobs, integrations or audit.
_PRODUCT_VIEWS = {
    ANALYTICS_VIEW, RESIDENTS_VIEW, RECORDS_VIEW, DOCUMENTS_VIEW, EVIDENCE_VIEW, GUIDELINES_VIEW,
    RULES_VIEW, CAREGAPS_VIEW, CAPACITY_VIEW, ENGAGEMENT_VIEW, APPOINTMENTS_VIEW, CLOSURE_VIEW, CONSENT_VIEW,
}  # fmt: skip

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.POPULATION_ADMIN: frozenset(
        _PRODUCT_VIEWS
        | {AUDIT_VIEW, PROCESSING_EXECUTE, CAPACITY_MANAGE, CAREGAPS_MANAGE, CAREGAPS_CLOSE, CONSENT_MANAGE}
    ),
    Role.CARE_COORDINATOR: frozenset({
        ANALYTICS_VIEW_LIMITED, RESIDENTS_VIEW, RECORDS_VIEW, GUIDELINES_VIEW, RULES_VIEW, CAREGAPS_VIEW,
        CAPACITY_VIEW, ENGAGEMENT_VIEW, APPOINTMENTS_VIEW, CLOSURE_VIEW, CONSENT_VIEW, AUDIT_VIEW_SCOPED,
        CAREGAPS_MANAGE, CAREGAPS_CLOSE, CONSENT_MANAGE,
    }),
    Role.CLINICAL_REVIEWER: frozenset({
        ANALYTICS_VIEW, RESIDENTS_VIEW, RECORDS_VIEW, DOCUMENTS_VIEW, EVIDENCE_VIEW, GUIDELINES_VIEW,
        RULES_VIEW, CAREGAPS_VIEW, CLOSURE_VIEW, CONSENT_VIEW, AUDIT_VIEW_SCOPED,
        EVIDENCE_REVIEW, GUIDELINES_APPROVE, CAREGAPS_REVIEW,
    }),
    # Sees every page (all product views + System pages); clinical actions stay with clinical_reviewer.
    Role.SYSTEM_ADMIN: frozenset(_PRODUCT_VIEWS | {
        JOBS_VIEW, INTEGRATIONS_VIEW, AUDIT_VIEW, PROCESSING_EXECUTE, CAPACITY_MANAGE, CAREGAPS_MANAGE,
        SYSTEM_CONFIGURE,
    }),
    # Judges (ADR-060): every product view, nothing internal (no jobs, integrations, audit), no writes.
    Role.OBSERVER: frozenset(_PRODUCT_VIEWS),
}  # fmt: skip

ALL_PERMISSIONS: frozenset[str] = frozenset().union(*ROLE_PERMISSIONS.values())


def permissions_for(role: str) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())  # unknown role -> nothing


def has_permission(role: str, permission: str) -> bool:
    return permission in permissions_for(role)
