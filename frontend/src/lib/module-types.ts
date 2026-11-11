// Module shapes. Mirrors the Pydantic *Out models in backend/anticipa/{evidence,guidelines,caregaps,planning}/routes.py
// (read 2026-11-11). Residents, analytics/population-overview and communications have NO backend route yet: their
// shapes follow the DB models and Docs/05 and are ASSUMED (fields optional). Replace with `npm run gen:types`
// output (openapi-typescript) once the API runs.

export type SyntheticStatus = "synthetic" | "simulated";

/** GET /analytics/population-overview (05 §16.1). ASSUMED: no backend route yet. Missing means "Not available". */
export interface PopulationOverview {
  dataset_version?: string | null;
  as_of?: string | null;
  generated_residents?: number | null;
  ingested_residents?: number | null;
  normalized_residents?: number | null;
  rule_evaluated_residents?: number | null;
  ai_processed_residents?: number | null;
  documents?: { registered?: number | null; processed?: number | null; pending?: number | null; failed?: number | null } | null;
}

/** GET /residents item (columns of `residents`). ASSUMED: no backend route yet. */
export interface Resident {
  id: string;
  source_patient_id?: string | null;
  birth_date: string | null;
  deceased_date?: string | null;
  administrative_sex: string | null;
  preferred_language: string | null;
  district_code: string | null;
  synthetic_status: SyntheticStatus;
  open_care_gap_count?: number | null;
}

export interface FamilyLink {
  id: string;
  relationship_type: string;
  relationship_direction?: string | null;
  related_resident_id: string | null;
  related_person_source_id?: string | null;
  source_type: string;
  confidence_status: string; // verified | uncertain | unknown
  synthetic_status: SyntheticStatus;
}

/** GET /residents/{id} (05 §6.2). ASSUMED. */
export interface ResidentDetail extends Resident {
  source_system?: string | null;
  dataset_id?: string | null;
  record_summary?: { category: string; count: number }[] | null;
  family?: FamilyLink[] | null;
  evidence_summary?: Record<string, number> | null; // keyed by validation_status
  open_care_gaps?: CareGap[] | null;
}

/** GET /residents/{id}/records item (columns of `clinical_records`). ASSUMED. */
export interface ClinicalRecord {
  id: string;
  resource_type: string;
  category: string;
  code_system: string | null;
  code: string | null;
  display: string | null;
  normalized_display?: string | null;
  effective_start: string | null;
  effective_end?: string | null;
  status: string | null;
  source_reference: string;
  synthetic_status: SyntheticStatus;
}

/** GET /residents/{id}/timeline item. ASSUMED. */
export interface TimelineEntry {
  id: string;
  occurred_at: string | null;
  kind: string; // record | evidence | workflow | ...
  title: string;
  detail?: string | null;
  source_reference?: string | null;
  synthetic_status?: SyntheticStatus;
}

// ---- evidence (backend/anticipa/evidence/routes.py: SourceOut, FactOut, FactDetail) ----
export interface EvidenceSource {
  id: string;
  source_type: string;
  document_id: string | null;
  quote: string | null;
  start_offset: number | null; // Unicode code points (Python str indices), end-exclusive
  end_offset: number | null;
  content_hash: string | null;
  verification_status: string; // verified_exact | verified_normalized | span_mismatch | quote_not_found | source_missing | verification_error
  location_ambiguous: boolean;
  all_spans: unknown[] | null;
}

export interface EvidenceFact {
  id: string;
  resident_id: string;
  category: string;
  fact_type: string;
  value: Record<string, unknown>; // includes original_text
  subject_type: string; // resident | family_member | other | unknown
  relationship_type: string | null;
  assertion_status: string; // positive | negative | uncertain | conditional
  temporal_status: string; // current | historical | planned | unknown
  extraction_confidence: string | null;
  uncertainty_note: string | null;
  validation_status: string; // pending | schema_valid | source_verified | accepted | requires_review | rejected | failed
  review_status: string;
  review_reasons: string[];
  extraction_method: string;
  model_provider: string | null;
  model_id: string | null;
  prompt_version: string | null;
  synthetic_status: SyntheticStatus;
  extracted_at: string;
  sources: EvidenceSource[];
}

/** GET /evidence/{id}?context=N: text around each quote, so offsets can be shown in context without the whole document. */
export interface EvidenceContext {
  source_id: string;
  before: string;
  quote: string;
  after: string;
}
export interface EvidenceDetail extends EvidenceFact {
  document: { id: string; document_type: string; language: string | null; created_on: string | null } | null;
  context: EvidenceContext[];
}

/** POST /evidence/{id}/review result. */
export interface ReviewResult {
  id: string;
  validation_status: string;
  review_status: string;
  previous_validation_status: string;
  changed: boolean;
}

// ---- guidelines (backend/anticipa/guidelines/routes.py) ----
export interface Rule {
  id: string;
  guideline_version_id: string;
  rule_key: string;
  version: number;
  name: string;
  description: string | null;
  care_activity: string;
  priority: string;
  rule_language: string;
  content_hash: string;
  approval_status: string; // draft | in_review | approved | retired
  created_at: string;
}
export interface GuidelineVersion {
  id: string;
  guideline_id: string;
  version: number;
  review_status: string; // draft | in_review | approved | retired
  source_reference: string | null;
  source_publication_date: string | null;
  effective_from: string;
  effective_until: string | null;
  disclaimer: string | null;
  change_summary: string | null;
  content_hash: string | null;
  created_at: string;
  submitted_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
  retired_at: string | null;
  retirement_reason: string | null;
}
export interface Guideline {
  id: string;
  guideline_key: string;
  name: string;
  description: string | null;
  topic: string | null;
  authoritative_source: string | null;
  source_url: string | null;
  created_at: string;
  updated_at: string;
  versions: GuidelineVersion[]; // newest first
}

// ---- care gaps (backend/anticipa/caregaps/routes.py: CaseOut, CaseDetailOut, HistoryOut) ----
export interface CareGap {
  id: string;
  resident_id: string;
  district_code: string | null;
  evaluation_id: string;
  guideline_version_id: string;
  rule_version_id: string;
  care_activity: string;
  status: string; // identified | pending_review | approved_for_planning | awaiting_capacity | planned | outreach_pending | ...
  priority: string; // routine | elevated | high
  source_outcome: string;
  assigned_coordinator_id: string | null;
  planning_run_id: string | null;
  opened_at: string;
  updated_at: string;
  closed_at: string | null;
  closure_reason: string | null;
  synthetic_status: SyntheticStatus;
}
export interface CaseHistory {
  id: string;
  from_status: string | null;
  to_status: string;
  actor_id: string | null;
  actor_role: string | null;
  reason: string | null;
  details: Record<string, unknown>;
  occurred_at: string;
}
export interface CareGapDetail extends CareGap {
  resident: { id: string; district_code?: string | null; preferred_language?: string | null; administrative_sex?: string | null; birth_date?: string | null; synthetic_status?: string };
  evaluation: {
    id?: string;
    outcome?: string;
    outcome_class?: string;
    evaluation_date?: string;
    evaluated_at?: string;
    explanation?: unknown;
    evidence_references?: unknown;
    missing_information?: unknown;
    exclusion_reasons?: unknown;
    execution_version?: string;
  };
  rule: {
    rule_key?: string;
    rule_version?: number;
    name?: string;
    care_activity?: string;
    content_hash?: string;
    guideline_key?: string | null;
    guideline_name?: string | null;
    guideline_version?: number | null;
    disclaimer?: string | null;
    source_reference?: string | null;
  };
  history: CaseHistory[];
  allowed_transitions: string[];
}
/** GET /care-gaps/{id}/communications item (05 §14.4). ASSUMED: no backend route yet. */
export interface Communication {
  id: string;
  occurred_at: string;
  channel?: string | null;
  event_type?: string | null;
  summary?: string | null;
}

// ---- planning (backend/anticipa/planning/routes.py) ----
export interface Clinic {
  id: string;
  clinic_key: string;
  name: string;
  district_code: string;
  address: string | null;
  timezone: string;
  operating_status: string;
  synthetic_status: SyntheticStatus;
  services: { service_key: string; service_name: string; appointment_duration_minutes: number; active: boolean }[];
}
/** GET /analytics/capacity: seats, not appointments. `reserved` = confirmed allocations. */
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
/** One row of RunOut.metrics.by_* tables. */
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
export interface PlanningRun {
  id: string;
  status: string; // queued | running | completed | failed | cancelled
  planning_period_start: string;
  planning_period_end: string;
  configuration_version: string;
  algorithm_version: string;
  seed: number;
  metrics: RunMetrics;
  error_summary: string | null;
  job_id: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  synthetic_status: SyntheticStatus;
}
export interface Allocation {
  id: string;
  planning_run_id: string;
  care_gap_case_id: string;
  resident_id: string;
  clinic_key: string;
  service_key: string;
  proposed_start_time: string;
  proposed_end_time: string;
  allocation_status: string; // proposed | confirmed | superseded
  priority: string | null;
  district_code: string | null;
  language: string | null;
  explanation: string | null;
  confirmed_at: string | null;
  synthetic_status: SyntheticStatus;
}
export interface Unallocated {
  id: string;
  care_gap_case_id: string;
  resident_id: string;
  service_key: string | null;
  priority: string | null;
  district_code: string | null;
  language: string | null;
  reason_code: string;
  explanation: string;
  details: Record<string, unknown>;
  synthetic_status: SyntheticStatus;
}
export interface ConfirmResult {
  allocation: Allocation;
  newly_confirmed: boolean;
  appointment_created: boolean; // always false in this build: confirming reserves a seat only
}
