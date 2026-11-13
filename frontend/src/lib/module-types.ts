// Module types. Almost everything here is an alias of the openapi-typescript output (`npm run gen:types`, generated from
// the running API on 2026-11-11), so a backend schema change surfaces as a compile error instead of a runtime surprise.
// Only endpoints the backend leaves untyped (dict payloads) have hand-written shapes, marked below.

import type { components } from "./api-types";

type S = components["schemas"];

export type SyntheticStatus = "synthetic" | "simulated";

// ---- data fabric ----
export type PopulationOverview = S["PopulationOverview"];
export type Resident = S["ResidentOut"];
export type ResidentDetail = S["ResidentDetail"];
export type ClinicalRecord = S["RecordOut"];
export type Family = S["FamilyOut"];
export type FamilyEdge = S["FamilyEdgeOut"];
export type TimelineEntry = S["TimelineEvent"];

// ---- evidence ----
export type EvidenceSource = S["SourceOut"];
export type EvidenceFact = S["FactOut"];
export type EvidenceDetail = S["FactDetail"];
export type EvidenceContext = S["ContextOut"];
export type ReviewResult = S["ReviewResult"];

// ---- guidelines ----
export type Rule = S["RuleOut"];
export type GuidelineVersion = S["VersionOut"];
export type Guideline = S["GuidelineOut"];

// ---- care gaps, engagement, closure ----
export type CareGap = S["CaseOut"];
export type CaseHistory = S["HistoryOut"];
export type CareGapDetail = S["CaseDetailOut"];
export type Communication = S["CommunicationOut"];
export type EngagementEvent = S["EventOut"];
export type ResidentResponse = S["ResponseOut"];
export type Appointment = S["AppointmentOut"];
export type CompletionEvidence = S["EvidenceOut"];
export type Closure = S["ClosureOut"];
export type OutreachDraft = S["DraftOut"];

/** CaseDetailOut.rule / .evaluation are free-form dicts in the API; these are the keys caregaps/routes.py `_detail` writes. */
export interface CareGapRule {
  rule_key?: string;
  rule_version?: number;
  name?: string;
  care_activity?: string;
  guideline_key?: string | null;
  guideline_name?: string | null;
  guideline_version?: number | null;
  disclaimer?: string | null;
  source_reference?: string | null;
}
export interface CareGapEvaluation {
  outcome?: string;
  outcome_class?: string;
  evaluation_date?: string;
  explanation?: unknown;
  evidence_references?: unknown;
  missing_information?: unknown;
  exclusion_reasons?: unknown;
}

/** GET /analytics/engagement: untyped dict in the API; keys from engagement/service.py `engagement_overview`. */
export interface EngagementOverview {
  simulated: boolean;
  note: string;
  communications: { total: number; by_delivery_state: Record<string, number>; by_channel: Record<string, number>; by_language: Record<string, number> };
  responses: { total: number; by_type: Record<string, number> };
  appointments: { total: number; by_status: Record<string, number> };
  completion_evidence: { total: number; by_verification_status: Record<string, number> };
  cases: { by_status: Record<string, number> };
  funnel: {
    cases_contacted: number;
    cases_delivered: number;
    cases_accepted: number;
    cases_with_appointment: number;
    cases_attended: number;
    cases_with_verified_evidence: number;
    cases_completed: number;
  };
}

// ---- planning ----
export type Clinic = S["ClinicOut"];
export type PlanningRun = S["RunOut"];
export type Allocation = S["AllocationOut"];
export type Unallocated = S["UnallocatedOut"];
export type ConfirmResult = S["ConfirmOut"];

/** GET /analytics/capacity: untyped dict in the API; keys from planning/service.py `capacity_overview`. Seats, not appointments. */
export interface CapacityOverview {
  simulated: boolean;
  window: { start: string | null; end: string | null; source: string };
  capacity: { total_seats: number; reserved_seats: number; remaining_seats: number; utilization: number | null; note: string };
  by_district: { district_code: string; total_seats: number; reserved_seats: number; remaining_seats: number; utilization: number | null; planned_seats: number }[];
  latest_run: null | {
    id: string;
    completed_at: string | null;
    cases_total: number | null;
    allocated: number | null;
    unallocated: number | null;
    coverage: number | null;
    plan_utilization: number | null;
    reason_counts: Record<string, number>;
  };
}
/** One row of RunOut.metrics.by_* (planning/solver.py `_metrics`). RunOut.metrics itself is an untyped dict. */
export interface GroupMetrics {
  demand: number;
  reachable: number;
  allocated: number;
  coverage: number | null;
  seats?: number;
  seats_allocated?: number;
  utilization?: number | null;
}
export interface RunMetrics {
  cases_total?: number;
  allocated?: number;
  unallocated?: number;
  coverage?: number | null;
  priority_weighted_coverage?: number | null;
  seats_total?: number;
  utilization?: number | null;
  by_district?: Record<string, GroupMetrics>;
  by_language?: Record<string, GroupMetrics>;
  by_priority?: Record<string, GroupMetrics>;
  reason_counts?: Record<string, number>;
}

/**
 * GET /analytics/evaluation (latest run). ASSUMED: the backend route does not exist yet. Shape follows the report.json
 * summary in Docs/09 §12.2 (without per-case data); every field is optional so a partial report still renders.
 */
export interface EvaluationReport {
  run_id?: string;
  created_at?: string;
  run_type?: string; // smoke | dev | comparison | test_final | scale
  status?: string; // complete | partial | failed
  synthetic_banner?: string;
  code?: { git_commit?: string; dirty?: boolean };
  dataset?: { dataset_version?: string };
  ground_truth?: { ground_truth_id?: string; frozen?: boolean };
  coverage?: { tier: string; target: number; actual_completed: number }[];
  metrics?: Record<string, { view?: string; value: number | null; numerator?: number; denominator?: number; n?: number; unit?: string; ci95?: [number, number] | null; status?: string; flags?: string[] }>;
  gates?: { gate_id: string; metric_id: string; comparator: string; threshold: number; threshold_status?: string; observed: number | null; result: string }[];
  safety_cases?: { metric_id: string; case_ref: string }[];
  exclusions?: { item: string; reason: string }[];
  failures?: { documents_failed?: number; by_error_category?: Record<string, number> };
  replay?: { replayed_calls?: number; live_calls?: number; recorded_cost_usd?: number; new_billed_cost_usd?: number };
  projections?: { name: string; formula?: string; value: number; label: string }[];
  limitations?: string[];
}

/**
 * GET /analytics/command-center (backend/anticipa/analytics/service.py `_command_center`). The route has no response_model,
 * so this is hand-typed from the builder. Every metric carries its definition; `source` is the backend's provenance tag.
 */
export interface CcMetric {
  key: string;
  label: string;
  value: number | null;
  definition: string;
  source: string;
  numerator?: number;
  denominator?: number;
}
export interface CcAlertRule {
  key: string;
  title: string;
  status: "alert" | "ok" | "no_data" | string;
  severity: "critical" | "warning" | null;
  value: number | null;
  threshold: number | null;
  message: string;
  definition: string;
  source: string;
}
export interface CcDistrict {
  due_residents?: number;
  due_share?: number | null;
  district_code: string;
  residents: number;
  cases: number;
  planned: number;
  completed: number;
  coverage: number | null;
  closure_rate: number | null;
}
export interface CcSpread {
  min: { district_code: string; value: number } | null;
  max: { district_code: string; value: number } | null;
  gap: number | null;
}
export interface CcActivity {
  kind: "audit" | "job" | string;
  at: string;
  title: string;
  outcome: string;
  resource_type?: string | null;
  resource_id?: string | null;
  actor_role?: string | null;
  processed_items?: number | null;
  total_items?: number | null;
}
/** Period comparison block of GET /analytics/command-center?compare=previous (analytics/service.py `_comparison`). */
export interface CcComparison {
  available: boolean;
  window_days: number;
  reason?: string;
  kpis?: Record<string, { current: number | null; previous: number | null; delta: number | null; delta_pp: number | null }>;
  flows?: Record<string, { current: number; previous: number; delta: number; delta_pct: number | null }>;
}
export interface CommandCenter {
  comparison?: CcComparison;
  generated_at: string;
  cache_ttl_seconds: number;
  limited: boolean;
  restricted: string[];
  dataset: { latest_version: string | null };
  kpis: CcMetric[];
  gaps_by_priority: { definition: string; source: string; items: { priority: string; open: number; completed: number; total: number }[] };
  closure_trend: { definition: string; source: string; days: number; points: { date: string; closed: number; opened: number }[] };
  equity: null | { definition: string; source: string; districts: CcDistrict[]; coverage: CcSpread; closure_rate: CcSpread };
  recent_activity: null | { definition: string; source: string; items: CcActivity[] };
  alert_rules: CcAlertRule[];
  alerts: CcAlertRule[];
}


/** GET /analytics/programmes and the `programmes` of each geography area (backend/anticipa/analytics/geography.py). */
export interface Programme {
  care_activity: string;
  label: string;
  cases_total: number;
  open_care_gaps: number;
  completed: number;
  in_outreach: number;
  due_residents: number;
  closure_rate: number | null;
  coverage: number | null;
}
export interface ProgrammesResponse {
  generated_at: string;
  population: number;
  programmes: Programme[];
}

/** One area of GET /analytics/geography (country, state or pilot district). Untyped dict in the API, typed from geography.py. */
export interface GeoArea {
  level: "country" | "state" | "district";
  code: string;
  iso_code: string;
  name: string;
  coverage_status: "pilot" | "no_coverage" | string;
  state?: string;
  state_code?: string;
  district_codes?: string[];
  population: number | null;
  due_residents: number | null;
  open_care_gaps: number | null;
  gaps_per_100: number | null;
  closure_rate: number | null;
  coverage: number | null;
  in_outreach: number | null;
  programmes: Programme[];
  message?: string | null;
}
export interface GeoResponse {
  level: string;
  code: string | null;
  generated_at: string;
  area: GeoArea;
  areas: GeoArea[];
  pilot: { districts: number; states: number; states_listed: number; definition: string };
}

/** GET /analytics/alerts/summary: the count behind the bell badge. */
export interface AlertsSummary {
  alerts_active: number;
  by_severity: Record<string, number>;
  items: { key: string; title: string; severity: string | null; message: string; source: string }[];
}
/** GET /meta/scope: the places this deployment serves. */
export interface ScopeMeta {
  country: { code: string; name: string };
  states: { code: string; name: string; iso_code: string; district_codes: string[] }[];
  districts: { code: string; name: string; state_code: string; state: string; facilities: number }[];
}
/** GET /residents/{id}/profile: place and programme coverage. */
export interface ResidentProfile {
  place: null | { district_code: string; city: string; state: string; iso_code: string };
  coverage: null | { programmes: { care_activity: string; label: string; status: string; status_label: string; case_status: string | null }[]; counts: Record<string, number>; definition: string };
}
