import type { IconName } from "@/components/ui/icons";

export const ROLE_LABELS: Record<string, string> = {
  population_admin: "Population administrator",
  care_coordinator: "Care coordinator",
  clinical_reviewer: "Clinical reviewer",
  system_admin: "System administrator",
  observer: "Read only observer",
};

export interface NavItem {
  href: string;
  label: string;
  icon: IconName;
  /** "main" items are the daily workflow; "system" items are configuration, operations and oversight. */
  group: "main" | "system";
  /**
   * Shown when the user holds ANY of these permissions (names from backend/anticipa/core/rbac.py).
   * The UI never infers access from the role name (ADR-060); the backend still enforces every call.
   */
  anyOf: readonly string[];
}

/** Product navigation: what a resident-facing programme team uses every day. */
export const NAV: readonly NavItem[] = [
  { href: "/", label: "Command Center", icon: "home", group: "main", anyOf: ["analytics.view", "analytics.view_limited"] },
  { href: "/population", label: "Population", icon: "users-round", group: "main", anyOf: ["analytics.view", "analytics.view_limited"] },
  { href: "/insights", label: "Insights", icon: "pie", group: "main", anyOf: ["analytics.view", "analytics.view_limited"] },
  { href: "/programs", label: "Programs", icon: "layers", group: "main", anyOf: ["caregaps.view"] },
  { href: "/residents", label: "Residents", icon: "users", group: "main", anyOf: ["residents.view"] },
  { href: "/care-gaps", label: "Care gaps", icon: "target", group: "main", anyOf: ["caregaps.view"] },
  { href: "/evidence-review", label: "Evidence review", icon: "sparkles", group: "main", anyOf: ["evidence.view"] },
  { href: "/guidelines", label: "Guidelines", icon: "scale", group: "main", anyOf: ["guidelines.view"] },
  { href: "/guidelines/studio", label: "Guideline Studio", icon: "flask", group: "main", anyOf: ["guidelines.view"] },
  { href: "/planning", label: "Capacity planning", icon: "calendar", group: "main", anyOf: ["capacity.view"] },
  { href: "/journey", label: "Care journey", icon: "journey", group: "main", anyOf: ["analytics.view", "analytics.view_limited"] },
  { href: "/operations", label: "Operations", icon: "gauge", group: "main", anyOf: ["capacity.view"] },
  { href: "/engagement", label: "Outreach", icon: "megaphone", group: "main", anyOf: ["engagement.view"] },
  // No evaluation permission exists in rbac.py yet (Docs/09 §10.3 proposes evaluation.read_summary); analytics.view is the closest grant.
  { href: "/evaluation", label: "Evaluation", icon: "chart", group: "main", anyOf: ["analytics.view"] },
  { href: "/reports", label: "Reports", icon: "file", group: "main", anyOf: ["analytics.view", "analytics.view_limited"] },
];

/**
 * System pages show the application's own backend (jobs, audit trail, integration health, live processing). They are
 * not product screens, so they live behind one small "System" entry and exist only for accounts holding
 * `system.configure`. Everyone else gets a "no access" state and no data is fetched.
 */
export const SYSTEM_PERMISSION = "system.configure";
export const SYSTEM_NAV: readonly NavItem[] = [
  { href: "/monitor", label: "Live monitor", icon: "activity", group: "system", anyOf: [SYSTEM_PERMISSION] },
  { href: "/jobs", label: "Jobs", icon: "layers", group: "system", anyOf: [SYSTEM_PERMISSION] },
  { href: "/audit", label: "Audit", icon: "scroll", group: "system", anyOf: [SYSTEM_PERMISSION] },
  { href: "/integrations", label: "Integrations", icon: "plug", group: "system", anyOf: [SYSTEM_PERMISSION] },
];
export const isSystemPath = (pathname: string) => SYSTEM_NAV.some((n) => pathname === n.href || pathname.startsWith(`${n.href}/`));

/** Write permissions the UI gates buttons on. Missing means the button is disabled, never hidden-then-guessed. */
export const PERM = {
  processingExecute: "processing.execute", // submit / retry / cancel jobs
  evidenceReview: "evidence.review", // accept / reject extracted facts
  guidelinesApprove: "guidelines.approve", // approve a guideline version
  capacityManage: "capacity.manage", // confirm a proposed allocation
} as const;

/**
 * Every permission that changes state or triggers processing (rbac.py). A user holding none of them is read-only
 * (observer); the shell shows a "Read-only access" badge. Individual actions still check their own permission.
 */
export const WRITE_PERMISSIONS: readonly string[] = [
  "processing.execute",
  "evidence.review",
  "guidelines.approve",
  "capacity.manage",
  "caregaps.manage",
  "caregaps.review",
  "caregaps.close",
  "system.configure",
];
