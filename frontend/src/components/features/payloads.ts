// Payloads of the read-model endpoints in backend/anticipa/analytics (routes.py, journey.py, service.py). They return
// plain dicts, so these mirror the keys the Python code writes. Sections a caller may not see come back null and are
// named in `restricted`. Callers: journey, pedigree, funnel, monitor, outreach preview. Purpose: show judges the screens.

import type { ResidentRecord } from "./types";

export type SourceKind = "live" | "synthetic" | "simulated" | "evaluation";

export interface JourneySource {
  id: string;
  document_id: string | null;
  quote: string | null;
  start_offset: number | null;
  end_offset: number | null;
  verification_status: string;
  location_ambiguous: boolean;
  source_reference?: string | null;
}
export interface JourneyFact {
  id: string;
  resident_id: string;
  category: string;
  fact_type: string;
  value: Record<string, unknown>;
  subject_type: string;
  relationship_type: string | null;
  assertion_status: string;
  temporal_status: string;
  age_at_onset?: number | null;
  extraction_confidence: string | null;
  uncertainty_note: string | null;
  validation_status: string;
  review_status: string;
  review_reasons: string[];
  extraction: { method: string; provider: string | null; model_id: string | null; prompt_version: string | null };
  extracted_at: string;
  synthetic_status: string;
  sources: JourneySource[];
  basis?: string; // pedigree only: stated_in_resident_document | relative_own_document
  reason?: string; // pedigree unattributed facts only
}
export interface JourneyDocument {
  id: string;
  document_type: string;
  language: string | null;
  created_on: string | null;
  registered_at: string | null;
  processing_status: string;
}
export interface JourneyEvaluation {
  id: string;
  is_latest: boolean;
  outcome: string;
  outcome_class: string;
  reason_code: string | null;
  review_status: string;
  explanation: Record<string, unknown>;
  evidence_references: unknown[];
  missing_information: unknown[];
  conflicting_information: unknown[];
  source_references: unknown[];
  exclusion_reasons: unknown[];
  evaluation_date: string;
  evaluated_at: string;
  execution_version: string;
  guideline: {
    id: string;
    key: string | null;
    name: string | null;
    version: number;
    version_id: string;
    care_activity: string | null;
    priority: string | null;
    review_status: string | null;
    source_reference: string | null;
    disclaimer: string | null;
  } | null;
  decisive_rule: { id: string; rule_version_id: string; name: string | null; version: number; rule_type: string | null } | null;
}
export interface JourneyClinic {
  id: string;
  key: string;
  name: string | null;
  district_code: string | null;
}
export interface JourneyAllocation {
  id: string;
  planning_run_id: string;
  clinic: JourneyClinic | null;
  service_key: string | null;
  priority: string | null;
  proposed_start_time: string;
  proposed_end_time: string;
  allocation_status: string;
  explanation: string | null;
  confirmed_at: string | null;
  simulated: boolean;
}
export interface JourneyAppointment {
  id: string;
  clinic: JourneyClinic | null;
  start_time: string;
  end_time: string;
  appointment_status: string;
  status_reason: string | null;
  simulated: boolean;
}
export interface JourneyMessage {
  id: string;
  channel: string;
  language: string;
  direction: string | null;
  template_key: string;
  template_version: number;
  body: string;
  delivery_state: string;
  sent_at: string | null;
  delivered_at: string | null;
  response_received_at: string | null;
  created_at: string;
  responses: { id: string; response_type: string; response_value: string | null; received_at: string }[];
  simulated: boolean;
}
export interface JourneyProof {
  id: string;
  activity: string;
  source_type: string;
  evidence_reference: string;
  verification_status: string;
  verification_method: string | null;
  reviewer_note: string | null;
  verified_at: string | null;
  created_at: string;
  simulated: boolean;
}
export interface JourneyCase {
  id: string;
  care_activity: string;
  status: string;
  priority: string;
  source_outcome: string;
  evaluation_id: string;
  planning_run_id: string | null;
  opened_at: string;
  updated_at: string;
  closed_at: string | null;
  closure_reason: string | null;
  history: { from_status: string | null; to_status: string; actor_role: string | null; reason: string | null; occurred_at: string }[];
  allocations: JourneyAllocation[] | null;
  appointments: JourneyAppointment[] | null;
  communications: JourneyMessage[] | null;
  completion_evidence: JourneyProof[] | null;
}
/** GET /residents/{id}/journey */
export interface JourneyPayload {
  synthetic_banner: string;
  sources: Record<string, string>;
  resident: ResidentRecord & { counts: { clinical_records: number; family_edges: number; documents: number; evidence_facts: number } };
  evidence: { definition: string; truncated: boolean; counts_by_validation_status: Record<string, number>; facts: JourneyFact[]; documents: JourneyDocument[] } | null;
  evaluations: { definition: string; truncated: boolean; items: JourneyEvaluation[] } | null;
  cases: { definition: string; items: JourneyCase[] } | null;
  restricted: string[];
}

/** GET /residents/{id}/pedigree */
export interface PedigreeRelative {
  edge_id: string;
  relationship: string;
  degree: number | null;
  degree_label: string | null;
  blood_relative: boolean | null;
  direction: string;
  reading: string;
  linked_resident: { id: string; age: number | null; administrative_sex: string | null; district_code: string | null; deceased: boolean } | null;
  related_person_source_id: string | null;
  source_type: string;
  confidence_status: string;
  cancer_history: {
    clinical_records: { id: string; display: string | null; code: string | null; status: string | null; effective_start: string | null; source_reference: string | null }[] | null;
    evidence_facts: JourneyFact[] | null;
  };
}
export interface Pedigree {
  synthetic_banner: string;
  resident: ResidentRecord;
  definition: string;
  relatives: PedigreeRelative[];
  unattributed_family_facts: JourneyFact[] | null;
  unattributed_note: string;
  counts: { relatives: number; linked_relatives: number; relatives_with_cancer_history: number };
  restricted: string[];
}

/** GET /analytics/funnel */
export interface FunnelStage {
  key: string;
  label: string;
  count: number;
  cases: number | null;
  unit: string;
  share_of_residents: number | null;
  conversion_from_previous?: number | null;
  definition: string;
  source: SourceKind;
}
export interface Funnel {
  generated_at: string;
  cache_ttl_seconds: number;
  synthetic_banner: string;
  sources: Record<string, string>;
  note: string;
  stages: FunnelStage[];
  leakage: { definition: string; source: SourceKind; items: { status: string; cases: number }[] };
}

/** GET /analytics/processing */
export interface ProcessingJob {
  id: string;
  job_type: string;
  status: string;
  total_items: number | null;
  processed_items: number;
  succeeded_items: number;
  failed_items: number;
  percent_complete: number | null;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  completed_at: string | null;
  heartbeat_age_seconds: number | null;
  error_summary: string | null;
}
export interface PipelineAvailable {
  available: true;
  source: SourceKind;
  definition: string;
  dataset_version: string;
  state: "complete" | "running" | "idle";
  manifest_status: string | null;
  resident_target: number;
  batch_size: number;
  manifest_updated_at: string | null;
  idle_seconds: number | null;
  generation: {
    batches_planned: number;
    batches_completed: number;
    batches_running: number;
    batches_failed: number;
    residents_generated: number;
    percent: number | null;
    throughput_residents_per_hour: number | null;
    eta_hours: number | null;
    throughput_basis: string;
  };
  ingestion: {
    dataset_loaded: boolean;
    dataset_status?: string;
    batches_loaded: number;
    residents_loaded: number;
    profiles: { full: number; rules: number };
    profiles_basis?: string;
    throughput_residents_per_hour: number | null;
    throughput_basis?: string;
    eta_hours: number | null;
    percent: number | null;
  };
  eta_hours: number | null;
  eta_basis: string;
}
export interface Processing {
  generated_at: string;
  cache_ttl_seconds: number;
  synthetic_banner: string;
  pipeline: PipelineAvailable | { available: false; source: SourceKind; dataset_version: string | null; message: string; definition: string };
  jobs: { source: SourceKind; definition: string; by_status: Record<string, number>; failed_last_24h: number; active: ProcessingJob[]; recent: ProcessingJob[] };
  ai_spend: {
    source: SourceKind;
    definition: string;
    total_usd: number;
    by_provider: Record<string, number>;
    models: { provider: string; model_id: string; calls: number; cost_usd: number; input_tokens: number; output_tokens: number; cache_hits: number; failed_calls: number }[];
    invocations: number;
    anthropic: {
      spend_usd: number;
      run_budget_usd: number;
      largest_run_spend_usd: number;
      largest_run_share_of_budget: number | null;
      run_budget_basis: string;
      spent_today_usd: number;
      daily_budget_usd: number;
      daily_share_of_budget: number | null;
    };
    nebius: { enabled: boolean; spend_usd: number; credit_usd: number; remaining_usd: number; share_of_credit_used: number | null };
  };
  documents: { source: SourceKind; definition: string; documents_total: number; documents_by_status: Record<string, number>; evidence_facts_total: number; evidence_facts_by_validation_status: Record<string, number> };
}

/** GET /engagement/preview?case_id= */
export interface OutreachPreviewData {
  care_gap_case_id: string;
  resident_id: string;
  care_activity: string;
  template_key: string;
  template_version: number;
  resident_preferred_language: string | null;
  default_language: string;
  default_language_source: string;
  previews: {
    language: string;
    direction: string;
    activity_label: string;
    body: string;
    characters: number;
    constraints_checked: boolean;
    is_resident_preferred_language: boolean;
    is_default_language: boolean;
    template_stored: boolean;
    template_approved: boolean | null;
  }[];
  ai_calls: number;
  simulated: boolean;
  notice: string;
}

/** GET /analytics/command-center (analytics/service.py `_command_center`). */
export interface Metric {
  key: string;
  label: string;
  value: number | null;
  definition: string;
  source: SourceKind;
  numerator?: number;
  denominator?: number;
}
export interface Alert {
  key: string;
  title: string;
  status: string;
  severity: string | null;
  value: number | null;
  threshold: number | null;
  message: string;
  definition: string;
}
export interface DistrictRow {
  district_code: string;
  residents: number;
  cases: number;
  planned: number;
  completed: number;
  coverage: number | null;
  closure_rate: number | null;
}
export interface CommandCenter {
  generated_at: string;
  limited: boolean;
  restricted: string[];
  kpis: Metric[];
  gaps_by_priority: { definition: string; items: { priority: string; open: number; completed: number; total: number }[] };
  closure_trend: { days: number; points: { date: string; closed: number; opened: number }[] };
  equity: { definition: string; districts: DistrictRow[]; coverage: { min: { district_code: string; value: number } | null; max: { district_code: string; value: number } | null; gap: number | null }; closure_rate: { min: { district_code: string; value: number } | null; max: { district_code: string; value: number } | null; gap: number | null } } | null;
  alert_rules: Alert[];
  alerts: Alert[];
}

/** GET /analytics/programmes (analytics/geography.py `_programmes`): one row per care activity, from a precomputed snapshot. */
export interface ProgrammeRow {
  care_activity: string;
  label: string;
  cases_total: number;
  open_care_gaps: number;
  completed: number;
  in_outreach: number;
  due_residents: number;
  closure_rate: number | null;
  coverage: number | null;
  up_to_date_residents: number;
  screening_coverage: number | null;
}
export interface Programmes {
  generated_at: string;
  population: number;
  programmes: ProgrammeRow[];
  definitions?: Record<string, { label: string; definition: string }>;
}

/** GET /analytics/geography?level=country|state&code= (analytics/geography.py): precomputed areas with care gap measures. */
export interface GeoArea {
  level: "country" | "state" | "district";
  code: string;
  name: string;
  coverage_status?: "pilot" | "no_coverage";
  district_codes?: string[];
  state?: string;
  population: number;
  due_residents: number;
  open_care_gaps: number;
  cases_total: number;
  completed: number;
  in_outreach: number;
  closure_rate: number | null;
  coverage: number | null;
  programmes: ProgrammeRow[];
}
export interface Geography {
  level: string;
  code: string | null;
  area: GeoArea;
  areas: GeoArea[];
}

/** GET /analytics/population-explorer (analytics/geography.py `_explorer`): a slice of the population and its pyramid. */
export interface PyramidCell {
  age_band: string;
  sex: string;
  residents: number;
  open_care_gaps: number;
  due_residents: number;
}
export interface ExplorerSlice {
  population: number;
  due_residents: number;
  open_care_gaps: number;
  cases_total: number;
  completed: number;
  in_outreach: number;
  attended?: number;
  gaps_per_100: number | null;
  closure_rate: number | null;
  coverage: number | null;
}
export interface PopulationExplorer {
  available: boolean;
  reason?: string;
  filters: { district: string | null; age_band: string | null; sex: string | null };
  options: { districts: { code: string; name: string; state: string }[]; age_bands: string[]; sexes: string[] };
  slice: ExplorerSlice | null;
  pyramid: PyramidCell[];
  definitions: Record<string, { label: string; definition: string }>;
}

/** The `totals` and note of GET /meta/scope beyond what the shell types carry. */
export interface ScopeTotals {
  totals?: { countries: number; states_with_coverage: number; states_listed: number; districts: number; facilities: number };
  facilities_source?: string;
  not_available?: Record<string, string>;
}

/** GET /analytics/impact (analytics/impact.py): stored baseline report, read only. Only the fields the page reads are typed. */
export interface ImpactRatio {
  numerator: number;
  denominator: number;
  value: number | null;
}
export interface ImpactTruthSide {
  truly_due: { found_as_due: number; missed_as_not_due: number; pairs: number; sent_to_review: number };
  truly_not_due: { correctly_not_due: number; pairs: number; sent_to_review: number; unnecessary_outreach: number };
}
export interface ImpactTruth {
  pairs: number;
  rules_only: ImpactTruthSide;
  with_notes: ImpactTruthSide;
}
export interface ImpactUplift {
  case_opened_only_because_of_notes: number;
  residents_with_case_opened_only_because_of_notes?: number;
  case_pairs_rules_only: number;
  case_pairs_with_notes: number;
  changes: Record<string, number>;
  pairs: number;
}
export interface ImpactPlan {
  cases: number;
  allocated: number;
  unallocated: number;
  seats_total: number;
  allocation_rate: ImpactRatio;
  seat_utilisation_services_with_cases: ImpactRatio;
  seats_services_with_cases: number;
  travel: { own_district_share: number; mean_tier: number };
  wait_days_from_horizon_start: { mean: number; median: number; p90: number };
  hard_constraint_violations: { count: number };
  coverage_by_service: Record<string, { allocated: number; coverage: number; demand: number }>;
}
type Pair = { first_come: number; ortools: number };
export interface ImpactReport {
  schema_version: string;
  generated_at: string;
  warnings?: string[];
  definitions?: Record<string, string>;
  limitations?: string[];
  measured_inputs: {
    residents: number;
    care_gap_cases: number;
    residents_with_a_case: number;
    ai_cohort: { residents: number; documents_extracted: number; evidence_facts: number; evidence_facts_per_resident: number };
    ai_extraction_cost: { calls: number; documents: number; total_cost_usd: number; escalation_cost_usd: number; usd_per_1000_documents: number; usd_per_1000_residents_of_the_ai_cohort: number; cost_basis: string };
  };
  rules_baseline: {
    scope: { residents: number; pairs: number; evaluation_date: string; ground_truth: { dev_residents: number } };
    uplift_from_reading_notes: { all: ImpactUplift } & Record<string, ImpactUplift>;
    against_planted_evidence_truth: ImpactTruth;
    against_planted_screening_truth: ImpactTruth;
  };
  planner_baseline: {
    first_come_first_served: ImpactPlan;
    ortools_cpsat: ImpactPlan;
    difference: { district_equity_gap_points: Pair; language_equity_gap_points: Pair; own_district_share: Pair; median_wait_days: Pair; hard_constraint_violations: Pair };
    inputs: { cases: number; distinct_residents: number; usable_seats: number };
  };
}
