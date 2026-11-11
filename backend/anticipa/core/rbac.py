"""Explicit role -> permission map (05 §4.3, ADR-060). Deny by default; scope limits ("assigned", "review")
are enforced
by the owning module on top of these permissions. Later waves extend ROLE_PERMISSIONS here, never with ad-hoc
role checks.

Naming makes read-only explicit: `<module>.view` (and `*.view_limited/_scoped/_redacted`) only read; anything
that
changes state, reviews, approves, closes, configures or triggers processing/AI uses
`.manage/.review/.approve/.close/
.execute/.configure`. The `observer` role (hackathon judges, ADR-060) holds exactly the plain `*.view`
permissions.

Mapping of the 05 §4.3 qualifiers:  Yes/Authorized -> full permission;  Limited/scoped -> the
`*_limited`/`*_scoped`
variant;  "No by default" -> not granted (grant explicitly if the owner decides).
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
JOBS_VIEW = "jobs.view"
INTEGRATIONS_VIEW = "integrations.view"
AUDIT_VIEW = "audit.view"
AUDIT_VIEW_SCOPED = "audit.view_scoped"  # own actions only

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
SYSTEM_CONFIGURE = "system.configure"

VIEW_PERMISSIONS: frozenset[str] = frozenset({
    ANALYTICS_VIEW, ANALYTICS_VIEW_LIMITED, RESIDENTS_VIEW, RECORDS_VIEW, DOCUMENTS_VIEW, EVIDENCE_VIEW,
    GUIDELINES_VIEW, RULES_VIEW, CAREGAPS_VIEW, CAPACITY_VIEW, ENGAGEMENT_VIEW, APPOINTMENTS_VIEW,
    CLOSURE_VIEW, JOBS_VIEW, INTEGRATIONS_VIEW, AUDIT_VIEW, AUDIT_VIEW_SCOPED,
})  # fmt: skip

_ADMIN_VIEWS = {
    ANALYTICS_VIEW, RESIDENTS_VIEW, RECORDS_VIEW, DOCUMENTS_VIEW, EVIDENCE_VIEW, GUIDELINES_VIEW,
    RULES_VIEW, CAREGAPS_VIEW, CAPACITY_VIEW, ENGAGEMENT_VIEW, APPOINTMENTS_VIEW, CLOSURE_VIEW,
    JOBS_VIEW, INTEGRATIONS_VIEW,
}  # fmt: skip
PLAIN_VIEW_PERMISSIONS: frozenset[str] = frozenset(p for p in VIEW_PERMISSIONS if p.endswith(".view"))

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.POPULATION_ADMIN: frozenset(
        _ADMIN_VIEWS | {AUDIT_VIEW, PROCESSING_EXECUTE, CAPACITY_MANAGE, CAREGAPS_MANAGE, CAREGAPS_CLOSE}
    ),
    Role.CARE_COORDINATOR: frozenset({
        ANALYTICS_VIEW_LIMITED, RESIDENTS_VIEW, RECORDS_VIEW, GUIDELINES_VIEW, RULES_VIEW, CAREGAPS_VIEW,
        CAPACITY_VIEW, ENGAGEMENT_VIEW, APPOINTMENTS_VIEW, CLOSURE_VIEW, INTEGRATIONS_VIEW, AUDIT_VIEW_SCOPED,
        CAREGAPS_MANAGE, CAREGAPS_CLOSE,
    }),
    Role.CLINICAL_REVIEWER: frozenset({
        ANALYTICS_VIEW, RESIDENTS_VIEW, RECORDS_VIEW, DOCUMENTS_VIEW, EVIDENCE_VIEW, GUIDELINES_VIEW,
        RULES_VIEW, CAREGAPS_VIEW, CLOSURE_VIEW, INTEGRATIONS_VIEW, AUDIT_VIEW_SCOPED,
        EVIDENCE_REVIEW, GUIDELINES_APPROVE, CAREGAPS_REVIEW,
    }),
    Role.SYSTEM_ADMIN: frozenset({
        ANALYTICS_VIEW, RESIDENTS_VIEW, RECORDS_VIEW, DOCUMENTS_VIEW, GUIDELINES_VIEW, RULES_VIEW,
        CAREGAPS_VIEW, CAPACITY_VIEW, ENGAGEMENT_VIEW, APPOINTMENTS_VIEW, CLOSURE_VIEW, JOBS_VIEW,
        INTEGRATIONS_VIEW, AUDIT_VIEW, PROCESSING_EXECUTE, CAPACITY_MANAGE, CAREGAPS_MANAGE, SYSTEM_CONFIGURE,
    }),
    # Hackathon judges (ADR-060): everything can be seen, nothing can be changed or triggered.
    Role.OBSERVER: PLAIN_VIEW_PERMISSIONS,
}  # fmt: skip

ALL_PERMISSIONS: frozenset[str] = frozenset().union(*ROLE_PERMISSIONS.values())


def permissions_for(role: str) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())  # unknown role -> nothing


def has_permission(role: str, permission: str) -> bool:
    return permission in permissions_for(role)
