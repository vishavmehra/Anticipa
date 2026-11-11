// Hand-written mirrors of Docs/17_IMPLEMENTATION_CONTRACTS.md §2/§3 and Docs/05 §17/§26.
// Replace with openapi-typescript output once /api/v1/openapi.json is generated.
// Fields not pinned by the contract are optional and marked "assumed".

/** Display only. The UI never gates behaviour on the role name; it reads `permissions` (ADR-060). */
export type Role = "population_admin" | "care_coordinator" | "clinical_reviewer" | "system_admin" | "observer";

/** GET /auth/me (backend/anticipa/api/auth.py `Me`): the backend decides role and permissions. */
export interface CurrentUser {
  id: string;
  email: string | null;
  display_name: string | null;
  role: Role | (string & {}); // tolerate roles added after this build
  permissions: string[]; // "<module>.<action>", see backend/anticipa/core/rbac.py
}

export const JOB_STATUSES = [
  "pending",
  "queued",
  "running",
  "partially_completed",
  "completed",
  "failed",
  "cancelled",
] as const;
export type JobStatus = (typeof JOB_STATUSES)[number];
export const ACTIVE_JOB_STATUSES: readonly string[] = ["pending", "queued", "running"];

/** processing_jobs row (contract §3). */
export interface Job {
  id: string;
  job_type: string;
  status: JobStatus;
  total_items: number | null;
  processed_items: number;
  succeeded_items: number;
  failed_items: number;
  retry_count: number;
  max_retries?: number | null;
  error_summary?: string | null;
  config_version?: string | null;
  input_scope?: Record<string, unknown> | null;
  checkpoint?: Record<string, unknown> | null; // only returned to users holding processing.execute
  cancel_requested?: boolean;
  created_by?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
}

/** GET /jobs/{id}/progress (backend `JobProgress`). */
export type JobProgress = Pick<
  Job,
  "status" | "total_items" | "processed_items" | "succeeded_items" | "failed_items" | "updated_at"
> & {
  job_id: string;
  percent_complete: number | null;
  item_status_counts: Record<string, number>;
  heartbeat_at: string | null;
};

/** GET /integrations item (backend `IntegrationHealth`). */
export interface Integration {
  key: string;
  name: string;
  configured: boolean;
  status: string; // healthy | degraded | unavailable | not_configured | authentication_failed | unknown
  detail: string | null;
  latency_ms: number | null;
  checked_at: string;
}

/** audit_events row (contract §3). */
export interface AuditEvent {
  id: string;
  occurred_at: string;
  actor_id: string | null;
  actor_role: string | null;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  outcome: string;
  request_id: string | null;
  details?: Record<string, unknown> | null;
}

export * from "./module-types";
