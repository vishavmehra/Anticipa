import { ok, paged, type ApiHandler } from "./helpers";

// Invented demo records for route-mocked tests. Not patient data and not metrics: every resident here is synthetic.
// Evidence, guideline, care-gap and planning shapes mirror backend/anticipa/*/routes.py (read 2026-11-11).
// Resident, population-overview and communication shapes are the frontend's ASSUMED shapes (no backend route yet).

export const RES_ID = "cccccccc-0000-4000-8000-000000000001";
export const RES_ID_2 = "cccccccc-0000-4000-8000-000000000002";
export const RES_ID_3 = "cccccccc-0000-4000-8000-000000000003";
export const GUIDELINE_ID = "dddddddd-0000-4000-8000-000000000001";
export const VERSION_ID = "dddddddd-0000-4000-8000-0000000000a2";
export const CASE_ID = "eeeeeeee-0000-4000-8000-000000000001";
export const RUN_ID = "ffffffff-0000-4000-8000-000000000001";
export const ALLOC_ID = "ffffffff-0000-4000-8000-0000000000a1";

/** Code-point offsets of `quote` inside `text`: the unit the backend (Python str indices) uses. */
function locate(text: string, quote: string) {
  const i = text.indexOf(quote);
  const start = Array.from(text.slice(0, i)).length;
  return { i, start, end: start + Array.from(quote).length };
}

export const DOCS = {
  en: {
    id: "99999999-0000-4000-8000-000000000001",
    language: "en",
    text: "Family history: mother diagnosed with breast cancer at age 46. Father has no history of colorectal cancer.",
    quote: "mother diagnosed with breast cancer at age 46",
  },
  ar: {
    id: "99999999-0000-4000-8000-000000000002",
    language: "ar",
    text: "التاريخ العائلي: أصيبت الأم بسرطان الثدي في عمر 46 سنة. لا يوجد تاريخ مرضي للأب.",
    quote: "أصيبت الأم بسرطان الثدي في عمر 46 سنة",
  },
  hi: {
    id: "99999999-0000-4000-8000-000000000003",
    language: "hi",
    text: "पारिवारिक इतिहास: माँ को 46 वर्ष की आयु में स्तन कैंसर का निदान हुआ था। पिता में कोई इतिहास नहीं है।",
    quote: "माँ को 46 वर्ष की आयु में स्तन कैंसर का निदान हुआ था",
  },
} as const;

const SRC_ID = { en: "88888888-0000-4000-8000-000000000001", ar: "88888888-0000-4000-8000-000000000002", hi: "88888888-0000-4000-8000-000000000003" } as const;

const source = (key: keyof typeof DOCS, over: Record<string, unknown> = {}) => {
  const d = DOCS[key];
  const { start, end } = locate(d.text, d.quote);
  return {
    id: SRC_ID[key],
    source_type: "document",
    document_id: d.id,
    quote: d.quote,
    start_offset: start,
    end_offset: end,
    content_hash: null,
    verification_status: "verified_exact",
    location_ambiguous: false,
    all_spans: null,
    ...over,
  };
};

const fact = (n: number, key: keyof typeof DOCS, over: Record<string, unknown> = {}) => ({
  id: `77777777-0000-4000-8000-00000000000${n}`,
  resident_id: RES_ID,
  category: "family_history",
  fact_type: "breast_cancer",
  value: { original_text: DOCS[key].quote },
  subject_type: "family_member",
  relationship_type: "mother",
  assertion_status: "positive",
  temporal_status: "historical",
  extraction_confidence: "high",
  uncertainty_note: null,
  validation_status: "requires_review",
  review_status: "pending",
  review_reasons: ["family_history_requires_review"],
  extraction_method: "llm",
  model_provider: "claude",
  model_id: "test-model",
  prompt_version: "evidence.extract@1",
  extracted_at: "2026-11-05T09:00:00Z",
  synthetic_status: "synthetic",
  sources: [source(key)],
  ...over,
});

export const FACTS = [
  fact(1, "en"),
  fact(2, "ar"),
  fact(3, "hi", { sources: [source("hi", { verification_status: "verified_normalized" })] }),
  fact(4, "en", {
    fact_type: "colorectal_cancer",
    subject_type: "other",
    relationship_type: null,
    assertion_status: "negative",
    sources: [source("en", { id: "88888888-0000-4000-8000-0000000000e4", verification_status: "quote_not_found" })],
  }),
];

/** GET /evidence/{id}?context=N: the excerpt around each quote, as the backend returns it. */
const detailHandler = (f: (typeof FACTS)[number], key: keyof typeof DOCS): ApiHandler => () => {
  const d = DOCS[key];
  const { i } = locate(d.text, d.quote);
  return {
    body: ok({
      ...f,
      document: { id: d.id, document_type: "clinical_note", language: d.language, created_on: null },
      invocation: null,
      review: null,
      context: [{ source_id: f.sources[0].id, before: d.text.slice(0, i), quote: d.quote, after: d.text.slice(i + d.quote.length) }],
    }),
  };
};

const resident = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  source_patient_id: "5a1c0e5e-1111-4222-8333-444455556666",
  birth_date: "1978-03-14",
  deceased_date: null,
  administrative_sex: "female",
  preferred_language: "en",
  district_code: "D03",
  synthetic_status: "synthetic",
  open_care_gap_count: 1,
  ...over,
});
export const RESIDENTS = [
  resident(RES_ID),
  resident(RES_ID_2, { administrative_sex: "male", birth_date: "1962-11-02", preferred_language: "ar", district_code: "D07", open_care_gap_count: 0 }),
  resident(RES_ID_3, { birth_date: "1990-07-30", preferred_language: "hi", district_code: "D01", open_care_gap_count: 2 }),
];

const CASE = {
  id: CASE_ID,
  resident_id: RES_ID,
  district_code: "D03",
  evaluation_id: "12121212-0000-4000-8000-000000000001",
  guideline_version_id: VERSION_ID,
  rule_version_id: "13131313-0000-4000-8000-000000000001",
  care_activity: "breast_cancer_screening",
  status: "awaiting_capacity",
  priority: "high",
  source_outcome: "eligible",
  assigned_coordinator_id: null,
  planning_run_id: null,
  opened_at: "2026-11-04T10:00:00Z",
  updated_at: "2026-11-05T08:00:00Z",
  closed_at: null,
  closure_reason: null,
  synthetic_status: "synthetic",
};

const VERSIONS = [
  { id: VERSION_ID, guideline_id: GUIDELINE_ID, version: 2, review_status: "in_review", source_reference: "Demo source reference 2026", source_publication_date: "2026-02-01", effective_from: "2026-11-15", effective_until: null, population_scope: {}, disclaimer: "Demo disclaimer: preventive-care opportunity, not a diagnosis.", change_summary: null, content_hash: null, created_at: "2026-11-02T09:00:00Z", submitted_at: "2026-11-03T09:00:00Z", approved_by: null, approved_at: null, retired_at: null, retirement_reason: null },
  { id: "dddddddd-0000-4000-8000-0000000000a1", guideline_id: GUIDELINE_ID, version: 1, review_status: "approved", source_reference: "Demo source reference 2024", source_publication_date: "2024-01-15", effective_from: "2026-10-20", effective_until: null, population_scope: {}, disclaimer: null, change_summary: null, content_hash: null, created_at: "2026-10-01T09:00:00Z", submitted_at: "2026-10-10T09:00:00Z", approved_by: null, approved_at: "2026-10-20T09:00:00Z", retired_at: null, retirement_reason: null },
];
const GUIDELINE = {
  id: GUIDELINE_ID,
  guideline_key: "breast-cancer-screening-demo",
  name: "Breast cancer screening (demo guideline)",
  description: "Demonstration guideline used for UI tests.",
  topic: "Cancer screening",
  authoritative_source: "Demo source reference",
  source_url: null,
  created_at: "2026-10-01T09:00:00Z",
  updated_at: "2026-11-03T12:00:00Z",
  versions: VERSIONS,
};

export const MODULE_HANDLERS: Record<string, ApiHandler> = {
  "GET /analytics/population-overview": () => ({
    body: ok({
      dataset_version: "syn-20261105-s42",
      as_of: "2026-11-05T09:14:31Z",
      generated_residents: 1200,
      ingested_residents: 1200,
      normalized_residents: 900,
      rule_evaluated_residents: 600,
      ai_processed_residents: null,
      documents: { registered: 240, processed: 120, pending: 100, failed: 20 },
    }),
  }),
  "GET /residents": () => ({ body: paged(RESIDENTS, 1, 25, 1200) }),
  [`GET /residents/${RES_ID}`]: () => ({
    body: ok({
      ...RESIDENTS[0],
      source_system: "synthea",
      record_summary: [
        { category: "condition", count: 12 },
        { category: "observation", count: 148 },
      ],
      family: [
        { id: "f1", relationship_type: "mother", related_resident_id: RES_ID_2, related_person_source_id: null, source_type: "structured", confidence_status: "verified", synthetic_status: "synthetic" },
        { id: "f2", relationship_type: "maternal_grandmother", related_resident_id: null, related_person_source_id: "ext-77", source_type: "document", confidence_status: "uncertain", synthetic_status: "synthetic" },
      ],
      evidence_summary: { accepted: 2, requires_review: 4, rejected: 1 },
      open_care_gaps: [CASE],
    }),
  }),
  [`GET /residents/${RES_ID}/records`]: () => ({
    body: paged(
      [
        { id: "r1", resource_type: "Condition", category: "condition", code_system: "http://snomed.info/sct", code: "254837009", display: "Malignant tumor of breast", normalized_display: null, effective_start: "2021-05-02T00:00:00Z", status: "active", source_reference: "synthea://syn-20261105-s42/b1/Condition/c-1", synthetic_status: "synthetic" },
        { id: "r2", resource_type: "Observation", category: "laboratory", code_system: "http://loinc.org", code: "2093-3", display: "Total cholesterol", normalized_display: null, effective_start: "2025-02-11T00:00:00Z", status: "final", source_reference: "synthea://syn-20261105-s42/b1/Observation/o-9", synthetic_status: "synthetic" },
      ],
      1,
      25,
      2,
    ),
  }),
  [`GET /residents/${RES_ID}/timeline`]: () => ({
    body: paged(
      [
        { id: "t1", occurred_at: "2021-05-02T00:00:00Z", kind: "record", title: "Condition recorded: malignant tumor of breast", detail: null, source_reference: "synthea://syn-20261105-s42/b1/Condition/c-1" },
        { id: "t2", occurred_at: "2026-11-05T09:00:00Z", kind: "evidence", title: "Family history extracted from a clinical note", detail: "Needs review.", source_reference: null },
      ],
      1,
      100,
      2,
    ),
  }),
  [`GET /residents/${RES_ID}/evidence`]: () => ({ body: paged(FACTS, 1, 10, 4) }),
  "GET /evidence": () => ({ body: paged(FACTS, 1, 10, 4) }),
  [`GET /evidence/${FACTS[0].id}`]: detailHandler(FACTS[0], "en"),
  [`GET /evidence/${FACTS[1].id}`]: detailHandler(FACTS[1], "ar"),
  [`GET /evidence/${FACTS[2].id}`]: detailHandler(FACTS[2], "hi"),
  "GET /guidelines": () =>
    ({
      body: paged(
        [
          GUIDELINE,
          { ...GUIDELINE, id: "dddddddd-0000-4000-8000-000000000002", guideline_key: "colorectal-demo", name: "Colorectal cancer screening (demo guideline)", versions: [{ ...VERSIONS[1], id: "dddddddd-0000-4000-8000-0000000000b1", guideline_id: "dddddddd-0000-4000-8000-000000000002" }] },
          { ...GUIDELINE, id: "dddddddd-0000-4000-8000-000000000003", guideline_key: "retired-demo", name: "Retired demo guideline", topic: "Immunisation", authoritative_source: null, versions: [] },
        ],
        1,
        25,
        3,
      ),
    }),
  [`GET /guidelines/${GUIDELINE_ID}`]: () => ({ body: ok(GUIDELINE) }),
  "GET /rules": () => ({
    body: paged(
      [{ id: "rule-1", guideline_version_id: VERSION_ID, rule_key: "screening_interval", version: 2, name: "Screening interval", description: "Deterministic Python rule.", care_activity: "breast_cancer_screening", priority: "high", rule_language: "python", content_hash: "abc", approval_status: "in_review", created_at: "2026-11-02T09:00:00Z" }],
      1,
      200,
      1,
    ),
  }),
  "GET /care-gaps": () => ({
    body: paged(
      [
        CASE,
        { ...CASE, id: "eeeeeeee-0000-4000-8000-000000000002", resident_id: RES_ID_3, care_activity: "colorectal_cancer_screening", status: "pending_review", priority: "routine", source_outcome: "requires_clinical_review", district_code: "D01" },
      ],
      1,
      25,
      2,
    ),
  }),
  [`GET /care-gaps/${CASE_ID}`]: () => ({
    body: ok({
      ...CASE,
      resident: { id: RES_ID, district_code: "D03", preferred_language: "en", administrative_sex: "female", birth_date: "1978-03-14", synthetic_status: "synthetic" },
      evaluation: { id: CASE.evaluation_id, outcome: "eligible", outcome_class: "eligible", evaluation_date: "2026-11-04", evaluated_at: "2026-11-04T09:58:00Z", explanation: { criteria: ["age 40-74", "first-degree relative with breast cancer"] }, evidence_references: [{ evidence_id: FACTS[0].id }], missing_information: [], exclusion_reasons: [], execution_version: "1" },
      rule: { rule_key: "screening_interval", rule_version: 2, name: "Screening interval", care_activity: "breast_cancer_screening", content_hash: "abc", guideline_key: "breast-cancer-screening-demo", guideline_name: "Breast cancer screening (demo guideline)", guideline_version: 2, disclaimer: "Demo disclaimer: preventive-care opportunity, not a diagnosis.", source_reference: "Demo source reference 2026" },
      history: [
        { id: "h1", from_status: null, to_status: "identified", actor_id: null, actor_role: null, reason: "Rule evaluation", details: {}, occurred_at: "2026-11-04T10:00:00Z" },
        { id: "h2", from_status: "identified", to_status: "awaiting_capacity", actor_id: "11111111-1111-4111-8111-111111111111", actor_role: "population_admin", reason: "Approved for planning", details: {}, occurred_at: "2026-11-05T08:00:00Z" },
      ],
      allowed_transitions: ["approved_for_planning", "escalated", "pending_review", "planned", "cancelled"],
    }),
  }),
  [`GET /care-gaps/${CASE_ID}/communications`]: () => ({
    body: paged([{ id: "o1", occurred_at: "2026-11-05T11:00:00Z", channel: "sms", event_type: "outreach_draft", summary: "Reminder drafted (not sent)" }], 1, 100, 1),
  }),
  "GET /analytics/capacity": () => ({
    body: ok({
      simulated: true,
      synthetic_status: "simulated",
      window: { start: "2026-11-10T00:00:00Z", end: "2026-11-17T00:00:00Z", source: "latest_run" },
      capacity: { total_seats: 320, reserved_seats: 80, remaining_seats: 240, utilization: 0.25, note: "reserved = confirmed allocations; planned_seats are proposals from the latest run, not bookings" },
      by_district: [
        { district_code: "D03", total_seats: 120, reserved_seats: 30, remaining_seats: 90, utilization: 0.25, planned_seats: 60 },
        { district_code: "D07", total_seats: 200, reserved_seats: 50, remaining_seats: 150, utilization: 0.25, planned_seats: 90 },
      ],
      latest_run: { id: RUN_ID, completed_at: "2026-11-05T09:31:00Z", cases_total: 276, allocated: 240, unallocated: 36, coverage: 0.87, plan_utilization: 0.75, reason_counts: { no_capacity: 30, unreachable: 6 } },
    }),
  }),
  "GET /planning/clinics": () => ({
    body: paged(
      [
        { id: "c1", clinic_key: "north-1", name: "Demo Clinic North", district_code: "D03", address: null, timezone: "UTC", operating_status: "open", synthetic_status: "simulated", services: [{ service_key: "mammography", service_name: "Mammography", appointment_duration_minutes: 30, active: true }] },
        { id: "c2", clinic_key: "south-1", name: "Demo Clinic South", district_code: "D07", address: null, timezone: "UTC", operating_status: "closed", synthetic_status: "simulated", services: [] },
      ],
      1,
      25,
      2,
    ),
  }),
  "GET /planning/runs": () => ({
    body: paged(
      [
        {
          id: RUN_ID,
          status: "completed",
          planning_period_start: "2026-11-10T00:00:00Z",
          planning_period_end: "2026-11-17T00:00:00Z",
          configuration_version: "1",
          algorithm_version: "cp-sat-1",
          seed: 42,
          metrics: { cases_total: 276, allocated: 240, unallocated: 36 },
          error_summary: null,
          job_id: null,
          created_at: "2026-11-05T09:30:00Z",
          started_at: "2026-11-05T09:30:02Z",
          completed_at: "2026-11-05T09:31:00Z",
          synthetic_status: "simulated",
        },
      ],
      1,
      25,
      1,
    ),
  }),
  [`GET /planning/runs/${RUN_ID}`]: () => ({
    body: ok({
      id: RUN_ID,
      status: "completed",
      planning_period_start: "2026-11-10T00:00:00Z",
      planning_period_end: "2026-11-17T00:00:00Z",
      configuration_version: "1",
      algorithm_version: "cp-sat-1",
      seed: 42,
      metrics: {
        cases_total: 276,
        allocated: 240,
        unallocated: 36,
        coverage: 0.87,
        by_district: { D03: { demand: 100, reachable: 100, allocated: 90, coverage: 0.9 }, D07: { demand: 176, reachable: 150, allocated: 150, coverage: 0.85 } },
        by_language: { ar: { demand: 40, reachable: 40, allocated: 30, coverage: 0.75 }, en: { demand: 236, reachable: 236, allocated: 210, coverage: 0.89 } },
        reason_counts: { no_capacity: 30, unreachable: 6 },
      },
      error_summary: null,
      job_id: null,
      created_at: "2026-11-05T09:30:00Z",
      started_at: "2026-11-05T09:30:02Z",
      completed_at: "2026-11-05T09:31:00Z",
      synthetic_status: "simulated",
    }),
  }),
  [`GET /planning/runs/${RUN_ID}/allocations`]: () => ({
    body: paged(
      [
        { id: ALLOC_ID, planning_run_id: RUN_ID, care_gap_case_id: CASE_ID, resident_id: RES_ID, clinic_key: "north-1", service_key: "mammography", proposed_start_time: "2026-11-12T09:00:00Z", proposed_end_time: "2026-11-12T09:30:00Z", allocation_status: "proposed", priority: "high", district_code: "D03", language: "en", explanation: null, confirmed_at: null, synthetic_status: "simulated" },
        { id: "ffffffff-0000-4000-8000-0000000000a2", planning_run_id: RUN_ID, care_gap_case_id: CASE_ID, resident_id: RES_ID, clinic_key: "south-1", service_key: "mammography", proposed_start_time: "2026-11-13T10:30:00Z", proposed_end_time: "2026-11-13T11:00:00Z", allocation_status: "confirmed", priority: "routine", district_code: "D07", language: "ar", explanation: null, confirmed_at: "2026-11-06T09:00:00Z", synthetic_status: "simulated" },
      ],
      1,
      25,
      2,
    ),
  }),
  [`GET /planning/runs/${RUN_ID}/unallocated`]: () => ({
    body: paged(
      [
        { id: "u1", planning_run_id: RUN_ID, care_gap_case_id: CASE_ID, resident_id: RES_ID_3, service_key: "mammography", priority: "routine", district_code: "D01", language: "hi", reason_code: "no_capacity", explanation: "No open seats in district D01 during the planning period.", details: {}, synthetic_status: "simulated" },
        { id: "u2", planning_run_id: RUN_ID, care_gap_case_id: CASE_ID, resident_id: RES_ID_2, service_key: "mammography", priority: "routine", district_code: "D07", language: "ar", reason_code: "unreachable", explanation: "No clinic within the allowed travel tier.", details: {}, synthetic_status: "simulated" },
      ],
      1,
      25,
      2,
    ),
  }),
};
