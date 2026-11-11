export const ROLE_LABELS: Record<string, string> = {
  population_admin: "Population administrator",
  care_coordinator: "Care coordinator",
  clinical_reviewer: "Clinical reviewer",
  system_admin: "System administrator",
  observer: "Read-only observer",
};

export interface NavItem {
  href: string;
  label: string;
  /**
   * Shown when the user holds ANY of these permissions (names from backend/anticipa/core/rbac.py).
   * The UI never infers access from the role name (ADR-060); the backend still enforces every call.
   */
  anyOf: readonly string[];
}

export const NAV: readonly NavItem[] = [
  { href: "/", label: "Population", anyOf: ["analytics.view", "analytics.view_limited"] },
  { href: "/residents", label: "Residents", anyOf: ["residents.view"] },
  { href: "/evidence-review", label: "Evidence Review", anyOf: ["evidence.view"] },
  { href: "/guidelines", label: "Guidelines", anyOf: ["guidelines.view"] },
  { href: "/care-gaps", label: "Care Gaps", anyOf: ["caregaps.view"] },
  { href: "/planning", label: "Planning", anyOf: ["capacity.view"] },
  { href: "/engagement", label: "Engagement", anyOf: ["engagement.view"] },
  { href: "/jobs", label: "Jobs", anyOf: ["jobs.view"] },
  { href: "/audit", label: "Audit", anyOf: ["audit.view", "audit.view_scoped", "audit.view_redacted"] },
  { href: "/integrations", label: "Integrations", anyOf: ["integrations.view"] },
];

/** Write permissions the UI gates buttons on. Missing means the button is disabled, never hidden-then-guessed. */
export const PERM = {
  processingExecute: "processing.execute", // submit / retry / cancel jobs
  evidenceReview: "evidence.review", // accept / reject extracted facts
  guidelinesApprove: "guidelines.approve", // approve a guideline version
  capacityManage: "capacity.manage", // confirm a proposed allocation
} as const;
