import { ok, paged, type ApiHandler } from "./helpers";
import { conversationHandlers } from "./conversation-fixtures";

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
  dataset_version: "syn-20261105-s42",
  source_patient_id: "5a1c0e5e-1111-4222-8333-444455556666",
  birth_date: "1978-03-14",
  age: 48,
  age_band: "40-49",
  administrative_sex: "female",
  preferred_language: "en",
  district_code: "D03",
  deceased_date: null,
  synthetic_status: "synthetic",
  age_as_of: "2026-11-05",
  ...over,
});
export const RESIDENTS = [
  resident(RES_ID),
  resident(RES_ID_2, { administrative_sex: "male", birth_date: "1962-11-02", age: 64, age_band: "60-69", preferred_language: "ar", district_code: "D07" }),
  resident(RES_ID_3, { birth_date: "1990-07-30", age: 36, age_band: "30-39", preferred_language: "hi", district_code: "D01" }),
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
  { id: VERSION_ID, guideline_id: GUIDELINE_ID, version: 2, review_status: "in_review", source_reference: "Source reference 2026", source_publication_date: "2026-02-01", effective_from: "2026-11-15", effective_until: null, care_activity: "breast_cancer_screening", priority: "high", source_version: null, clinical_review_status: "pending", population_scope: {}, disclaimer: "This is a preventive care opportunity, not a diagnosis.", change_summary: null, content_hash: null, created_at: "2026-11-02T09:00:00Z", submitted_at: "2026-11-03T09:00:00Z", approved_by: null, approved_at: null, retired_at: null, retirement_reason: null },
  { id: "dddddddd-0000-4000-8000-0000000000a1", guideline_id: GUIDELINE_ID, version: 1, review_status: "approved", source_reference: "Source reference 2024", source_publication_date: "2024-01-15", effective_from: "2026-10-20", effective_until: null, care_activity: "breast_cancer_screening", priority: "high", source_version: null, clinical_review_status: "approved", population_scope: {}, disclaimer: null, change_summary: null, content_hash: null, created_at: "2026-10-01T09:00:00Z", submitted_at: "2026-10-10T09:00:00Z", approved_by: null, approved_at: "2026-10-20T09:00:00Z", retired_at: null, retirement_reason: null },
];
const GUIDELINE = {
  id: GUIDELINE_ID,
  guideline_key: "breast-cancer-screening-demo",
  name: "Breast cancer screening",
  description: "Screening guideline.",
  topic: "Cancer screening",
  authoritative_source: "Source reference",
  source_url: null,
  created_at: "2026-10-01T09:00:00Z",
  updated_at: "2026-11-03T12:00:00Z",
  versions: VERSIONS,
};

export const APPT_ID = "abababab-0000-4000-8000-000000000001";
export const EVID_ID = "cdcdcdcd-0000-4000-8000-000000000001";
const APPT = {
  id: APPT_ID, care_gap_case_id: CASE_ID, resident_id: RES_ID, clinic_id: "c1", clinic_service_id: "s1", capacity_slot_id: "slot1", planned_allocation_id: ALLOC_ID,
  clinic_key: "north-1", service_key: "mammography", start_time: "2026-11-12T09:00:00Z", end_time: "2026-11-12T09:30:00Z", appointment_status: "simulated_confirmed",
  status_reason: null, source_type: "manual", created_by: null, created_at: "2026-11-06T09:00:00Z", updated_at: "2026-11-06T09:00:00Z", synthetic_status: "simulated", simulated: true, notice: "Simulated.",
};
export const EVIDENCE = [
  { id: EVID_ID, care_gap_case_id: CASE_ID, activity: "breast_cancer_screening", source_type: "simulated_procedure_record", source_resource_type: null, source_resource_id: null, document_id: null, evidence_reference: "Simulated mammography record 77", verification_status: "pending", verification_method: null, reviewed_by: null, reviewer_note: null, verified_at: null, created_by: null, created_at: "2026-11-07T09:00:00Z", synthetic_status: "simulated", simulated: true },
];


// ---- geography (GET /analytics/geography) and programmes (GET /analytics/programmes)
const GEO_DISTRICTS = [
  ["D01", "Bhopal", "23", "Madhya Pradesh", 21480, 11820], ["D02", "Indore", "23", "Madhya Pradesh", 19820, 9210],
  ["D03", "Jabalpur", "23", "Madhya Pradesh", 20410, 13440], ["D04", "Gwalior", "23", "Madhya Pradesh", 18950, 10980],
  ["D05", "Pune", "27", "Maharashtra", 22140, 14210], ["D06", "Nagpur", "27", "Maharashtra", 19330, 9980],
  ["D07", "Lucknow", "09", "Uttar Pradesh", 20870, 12150], ["D08", "Varanasi", "09", "Uttar Pradesh", 18240, 8890],
  ["D09", "Bengaluru Urban", "29", "Karnataka", 19790, 11030], ["D10", "Mysuru", "29", "Karnataka", 18970, 9940],
] as const;
const PROGS = [
  { care_activity: "breast_cancer_screening", label: "Breast cancer screening", cases_total: 4200, open_care_gaps: 3700, completed: 500, in_outreach: 300, due_residents: 4100, closure_rate: 0.119, coverage: 0.7 },
  { care_activity: "colorectal_cancer_screening", label: "Colorectal cancer screening", cases_total: 3100, open_care_gaps: 2900, completed: 200, in_outreach: 150, due_residents: 3000, closure_rate: 0.065, coverage: 0.62 },
];
const geoMetrics = (pop: number, cases: number, extra: Record<string, unknown> = {}) => ({
  population: pop, due_residents: Math.round(cases * 0.8), open_care_gaps: Math.round(cases * 0.9), gaps_per_100: Math.round((cases / pop) * 1000) / 10,
  closure_rate: 0.11, coverage: 0.76, in_outreach: Math.round(cases * 0.07), programmes: PROGS, ...extra,
});
const distArea = (d: (typeof GEO_DISTRICTS)[number]) => ({ level: "district", code: d[0], iso_code: "IN-XX", name: d[1], state_code: d[2], state: d[3], coverage_status: "pilot", message: null, ...geoMetrics(d[4], d[5]) });
const stateAreas = [
  ...[["23", "IN-MP", "Madhya Pradesh"], ["27", "IN-MH", "Maharashtra"], ["09", "IN-UP", "Uttar Pradesh"], ["29", "IN-KA", "Karnataka"]].map(([code, iso, name]) => {
    const ds = GEO_DISTRICTS.filter((d) => d[2] === code);
    const pop = ds.reduce((t, d) => t + d[4], 0);
    const cases = ds.reduce((t, d) => t + d[5], 0);
    return { level: "state", code, iso_code: iso, name, type: "state", coverage_status: "pilot", district_codes: ds.map((d) => d[0]), message: null, ...geoMetrics(pop, cases) };
  }),
  ...[["32", "IN-KL", "Kerala"], ["30", "IN-GA", "Goa"]].map(([code, iso, name]) => ({ level: "state", code, iso_code: iso, name, type: "state", coverage_status: "no_coverage", district_codes: [], message: "No pilot district in this state: no residents are placed here.", ...geoMetrics(0, 0, { gaps_per_100: null, closure_rate: null, coverage: null, programmes: [] }) })),
];
const geoHandler: ApiHandler = (_req, url) => {
  const level = url.searchParams.get("level") ?? "country";
  const code = url.searchParams.get("code");
  const total = GEO_DISTRICTS.reduce((t, d) => [t[0] + d[4], t[1] + d[5]] as [number, number], [0, 0] as [number, number]);
  const base = { level, code, generated_at: "2026-11-12T18:30:00Z", pilot: { districts: 10, states: 4, states_listed: 6, definition: "Residents are placed in ten pilot districts." } };
  if (level === "state") {
    const st = stateAreas.find((x) => x.code === code || x.iso_code.endsWith(`-${code}`)) ?? stateAreas[0];
    return { body: ok({ ...base, area: st, areas: GEO_DISTRICTS.filter((d) => d[2] === st.code).map(distArea) }) };
  }
  if (level === "district") return { body: ok({ ...base, area: distArea(GEO_DISTRICTS.find((d) => d[0] === code) ?? GEO_DISTRICTS[0]), areas: [] }) };
  return { body: ok({ ...base, area: { level: "country", code: "IN", iso_code: "IN", name: "India", coverage_status: "pilot", ...geoMetrics(total[0], total[1]) }, areas: stateAreas }) };
};

export const MODULE_HANDLERS: Record<string, ApiHandler> = {
  ...conversationHandlers(CASE_ID),
  "GET /analytics/geography": geoHandler,
  [`GET /residents/${RES_ID}/consents`]: () => ({ body: ok({
    resident_id: RES_ID,
    effective: { outreach: { permitted_channels: ["sms", "whatsapp", "phone_call", "email"], denied_channels: [], blocked: false }, data_use_ai_processing: "permit" },
    records: [],
    default_note: "No consent has been recorded, so the approved default applies.",
    notice: "Consent records are kept for audit.",
  }) }),
  "GET /analytics/alerts/summary": () => ({ body: ok({ alerts_active: 1, by_severity: { critical: 0, warning: 1 }, items: [{ key: "capacity_utilisation", title: "Clinic capacity nearly full", severity: "warning", message: "93% of seats in the next 7 days are reserved.", source: "live" }] }) }),
  "GET /meta/scope": () => ({ body: ok({
    country: { code: "IN", name: "India" },
    states: [{ code: "23", name: "Madhya Pradesh", iso_code: "IN-MP", district_codes: ["D01", "D02"] }, { code: "27", name: "Maharashtra", iso_code: "IN-MH", district_codes: ["D05"] }],
    districts: [{ code: "D01", name: "Bhopal", state_code: "23", state: "Madhya Pradesh", facilities: 2 }, { code: "D02", name: "Indore", state_code: "23", state: "Madhya Pradesh", facilities: 1 }, { code: "D05", name: "Pune", state_code: "27", state: "Maharashtra", facilities: 2 }],
  }) }),
  [`GET /residents/${RES_ID}/profile`]: () => ({ body: ok({
    place: { district_code: "D03", city: "Jabalpur", state: "Madhya Pradesh", iso_code: "IN-MP" },
    coverage: { programmes: [{ care_activity: "breast_cancer_screening", label: "Breast cancer screening", status: "due", status_label: "Due now", case_status: "identified" }, { care_activity: "colorectal_cancer_screening", label: "Colorectal cancer screening", status: "up_to_date", status_label: "Up to date", case_status: null }], counts: { due: 1, up_to_date: 1 }, definition: "One row per program." },
  }) }),
  "GET /analytics/population-explorer": () => ({ body: ok({
    available: true,
    filters: { district: null, age_band: null, sex: null },
    options: { districts: [{ code: "D01", name: "Bhopal", state: "Madhya Pradesh" }], age_bands: ["30-39", "40-49"], sexes: ["female", "male"] },
    slice: { population: 1200, due_residents: 150, open_care_gaps: 90, cases_total: 100, completed: 10, in_outreach: 5, gaps_per_100: 7.5, closure_rate: 0.1, coverage: 0.5 },
    pyramid: [
      { age_band: "30-39", sex: "female", residents: 300, open_care_gaps: 20, due_residents: 30 }, { age_band: "30-39", sex: "male", residents: 100, open_care_gaps: 5, due_residents: 8 },
      { age_band: "40-49", sex: "female", residents: 400, open_care_gaps: 40, due_residents: 60 }, { age_band: "40-49", sex: "male", residents: 400, open_care_gaps: 25, due_residents: 52 },
    ],
    definitions: { due_residents: { label: "Due for care", definition: "Residents whose latest evaluation is eligible." } },
  }) }),
  "GET /analytics/programmes": () => ({ body: ok({ generated_at: "2026-11-12T18:30:00Z", population: 200000, programmes: PROGS }) }),
  "GET /analytics/population-overview": () => ({
    body: ok({
      dataset_version: "syn-20261105-s42",
      generator: "synthea 3.3.0",
      reference_date: "2026-11-05",
      metrics: [
        { key: "residents_total", label: "Residents", value: 1200, definition: "Rows in anticipa.residents for this dataset (synthetic residents; one row per Synthea patient)." },
        { key: "residents_with_family_links", label: "Residents with at least one family link", value: 900, definition: "Distinct residents with >= 1 row in anticipa.family_edges." },
        { key: "documents_total", label: "Registered documents", value: 240, definition: "Rows in anticipa.documents for this dataset's residents (synthetic documents)." },
      ],
      distributions: [
        { key: "age_band", label: "Age band", definition: "Completed years on 2026-11-05 (the dataset reference date), from birth_date.", buckets: [{ key: "30-39", count: 400, share: 0.3333 }, { key: "40-49", count: 800, share: 0.6667 }] },
        { key: "sex", label: "Administrative sex", definition: "residents.administrative_sex as generated by Synthea (FHIR gender).", buckets: [{ key: "female", count: 700, share: 0.5833 }, { key: "male", count: 500, share: 0.4167 }] },
      ],
      basis: { residents: "live count of anticipa.residents" },
      limited: false,
      notice: "Synthetic data: generated by Synthea and a seeded linker; no real person is represented.",
    }),
  }),
  "GET /analytics/command-center": () => ({
    body: ok({
      generated_at: "2026-11-12T10:30:00Z",
      cache_ttl_seconds: 15,
      limited: false,
      restricted: [],
      dataset: { latest_version: "syn-20261105-s42" },
      kpis: [
        { key: "residents_total", label: "Residents", value: 1200, definition: "Rows in residents.", source: "synthetic" },
        { key: "residents_with_open_gaps", label: "Residents with open care gaps", value: 300, definition: "Distinct residents with an open case.", source: "synthetic" },
        { key: "care_gaps_open", label: "Open care gaps", value: 320, definition: "Open cases.", source: "synthetic" },
        { key: "care_gaps_total", label: "Care gaps opened", value: 400, definition: "All cases.", source: "synthetic" },
        { key: "gaps_closed", label: "Gaps closed", value: 60, definition: "Completed cases.", source: "simulated" },
        { key: "closure_rate", label: "Closure rate", value: 0.15, definition: "Completed / all cases.", source: "simulated", numerator: 60, denominator: 400 },
        { key: "in_outreach", label: "Currently in outreach", value: 45, definition: "Outreach statuses.", source: "simulated" },
      ],
      gaps_by_priority: {
        definition: "Cases by priority.",
        source: "synthetic",
        items: [
          { priority: "high", open: 80, completed: 10, total: 90 },
          { priority: "elevated", open: 140, completed: 20, total: 160 },
          { priority: "routine", open: 100, completed: 30, total: 130 },
        ],
      },
      closure_trend: {
        definition: "Per UTC day.",
        source: "simulated",
        days: 7,
        points: [3, 5, 4, 8, 6, 9, 12].map((closed, i) => ({ date: `2026-11-${String(i + 6).padStart(2, "0")}`, closed, opened: closed + 4 })),
      },
      equity: {
        definition: "Per district.",
        source: "simulated",
        districts: [
          { district_code: "D01", residents: 120, cases: 40, planned: 30, completed: 6, coverage: 0.75, closure_rate: 0.15 },
          { district_code: "D03", residents: 130, cases: 50, planned: 30, completed: 5, coverage: 0.6, closure_rate: 0.1 },
          { district_code: "D07", residents: 110, cases: 30, planned: 27, completed: 8, coverage: 0.9, closure_rate: 0.27 },
        ],
        coverage: { min: { district_code: "D03", value: 0.6 }, max: { district_code: "D07", value: 0.9 }, gap: 0.3 },
        closure_rate: { min: { district_code: "D03", value: 0.1 }, max: { district_code: "D07", value: 0.27 }, gap: 0.17 },
      },
      recent_activity: {
        definition: "Recent audit events and jobs.",
        source: "live",
        items: [
          { kind: "audit", at: "2026-11-12T10:20:00Z", title: "caregap.status_change", outcome: "success", resource_type: "care_gap_case", resource_id: "x", actor_role: "population_admin" },
          { kind: "job", at: "2026-11-12T10:10:00Z", title: "planning.run", outcome: "completed", resource_type: "processing_job", resource_id: "y", processed_items: 276, total_items: 276 },
        ],
      },
      alert_rules: [
        { key: "capacity_utilisation", title: "Clinic capacity nearly full", status: "alert", severity: "warning", value: 0.93, threshold: 0.9, message: "93% of seats in the next 7 days are reserved.", definition: "Reserved seats / available seats.", source: "simulated" },
        { key: "jobs_failed", title: "Failed background jobs", status: "ok", severity: null, value: 0, threshold: 0, message: "No background job failed in the last 24 hours.", definition: "Failed jobs.", source: "live" },
        { key: "evidence_review_share", title: "Evidence awaiting review", status: "no_data", severity: null, value: null, threshold: 0.5, message: "No evidence facts have been extracted yet.", definition: "Pending / all facts.", source: "synthetic" },
      ],
      alerts: [
        { key: "capacity_utilisation", title: "Clinic capacity nearly full", status: "alert", severity: "warning", value: 0.93, threshold: 0.9, message: "93% of seats in the next 7 days are reserved.", definition: "Reserved seats / available seats.", source: "simulated" },
      ],
    }),
  }),
  "GET /residents": () => ({ body: paged(RESIDENTS, 1, 25, 1200) }),
  [`GET /residents/${RES_ID}`]: () => ({
    body: ok({
      ...RESIDENTS[0],
      record_counts: { condition: 12, laboratory: 148 },
      family_edge_count: 2,
      document_count: 4,
      notice: "Synthetic data: generated by Synthea and a seeded linker; no real person is represented.",
    }),
  }),
  [`GET /residents/${RES_ID}/family`]: () => ({
    body: ok({
      resident_id: RES_ID,
      edges: [
        { id: "f1", relationship_type: "mother", relationship_direction: "to_resident", related_resident: { id: RES_ID_2, age: 74, administrative_sex: "female", district_code: "D03" }, related_person_source_id: null, source_type: "linker", confidence_status: "verified", synthetic_status: "synthetic" },
        { id: "f2", relationship_type: "maternal_grandmother", relationship_direction: "to_resident", related_resident: null, related_person_source_id: "ext-77", source_type: "document", confidence_status: "uncertain", synthetic_status: "synthetic" },
      ],
      counts_by_relationship: { maternal_grandmother: 1, mother: 1 },
      reading: "Each edge reads: the related person is the resident's <relationship_type>.",
      notice: "Synthetic data: generated by Synthea and a seeded linker; no real person is represented.",
    }),
  }),
  [`GET /residents/${RES_ID}/records`]: () => ({
    body: paged(
      [
        { id: "r1", resource_type: "Condition", source_resource_id: "c-1", category: "condition", code_system: "http://snomed.info/sct", code: "254837009", display: "Malignant tumor of breast", effective_start: "2021-05-02T00:00:00Z", effective_end: null, status: "active", value: { tag: "breast_cancer" }, source_reference: "synthea://syn-20261105-s42/b1/Condition/c-1", synthetic_status: "synthetic" },
        { id: "r2", resource_type: "Observation", source_resource_id: "o-9", category: "laboratory", code_system: "http://loinc.org", code: "2093-3", display: "Total cholesterol", effective_start: "2025-02-11T00:00:00Z", effective_end: null, status: "final", value: null, source_reference: "synthea://syn-20261105-s42/b1/Observation/o-9", synthetic_status: "synthetic" },
      ],
      1,
      25,
      2,
    ),
  }),
  [`GET /residents/${RES_ID}/timeline`]: () => ({
    body: paged(
      [
        { event_id: "t2", source: "document", kind: "clinical_note", title: "clinical_note", code_system: null, code: null, occurred_at: "2026-11-05T09:00:00Z", ended_at: null, status: "processed", source_reference: null },
        { event_id: "t1", source: "clinical_record", kind: "condition", title: "Malignant tumor of breast", code_system: "http://snomed.info/sct", code: "254837009", occurred_at: "2021-05-02T00:00:00Z", ended_at: null, status: "active", source_reference: "synthea://syn-20261105-s42/b1/Condition/c-1" },
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
          { ...GUIDELINE, id: "dddddddd-0000-4000-8000-000000000002", guideline_key: "colorectal-demo", name: "Colorectal cancer screening", versions: [{ ...VERSIONS[1], id: "dddddddd-0000-4000-8000-0000000000b1", guideline_id: "dddddddd-0000-4000-8000-000000000002" }] },
          { ...GUIDELINE, id: "dddddddd-0000-4000-8000-000000000003", guideline_key: "retired-demo", name: "Retired guideline", topic: "Immunisation", authoritative_source: null, versions: [] },
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
      rule: { rule_key: "screening_interval", rule_version: 2, name: "Screening interval", care_activity: "breast_cancer_screening", content_hash: "abc", guideline_key: "breast-cancer-screening-demo", guideline_name: "Breast cancer screening", guideline_version: 2, disclaimer: "This is a preventive care opportunity, not a diagnosis.", source_reference: "Source reference 2026" },
      history: [
        { id: "h1", from_status: null, to_status: "identified", actor_id: null, actor_role: null, reason: "Rule evaluation", details: {}, occurred_at: "2026-11-04T10:00:00Z" },
        { id: "h2", from_status: "identified", to_status: "awaiting_capacity", actor_id: "11111111-1111-4111-8111-111111111111", actor_role: "population_admin", reason: "Approved for planning", details: {}, occurred_at: "2026-11-05T08:00:00Z" },
      ],
      allowed_transitions: ["approved_for_planning", "escalated", "pending_review", "planned", "cancelled"],
    }),
  }),
  [`GET /care-gaps/${CASE_ID}/communications`]: () => ({
    body: ok([
      {
        id: "e1", care_gap_case_id: CASE_ID, resident_id: RES_ID, channel: "sms", template_key: "preventive_care_reminder", template_version: 1, language: "ar",
        body: "مرحبا، حان موعد الفحص الوقائي الخاص بك.", delivery_state: "delivered", source_type: "simulation", job_id: null, sent_at: "2026-11-05T11:00:00Z", delivered_at: "2026-11-05T11:00:05Z",
        response_received_at: "2026-11-05T12:00:00Z", created_by: null, created_at: "2026-11-05T11:00:00Z", synthetic_status: "simulated", simulated: true, notice: "Simulated.", direction: "rtl",
        responses: [{ id: "x1", communication_event_id: "e1", care_gap_case_id: CASE_ID, resident_id: RES_ID, response_type: "accept", response_value: null, received_at: "2026-11-05T12:00:00Z", source_reference: null, created_at: "2026-11-05T12:00:00Z", synthetic_status: "simulated", simulated: true }],
      },
    ]),
  }),
  "GET /appointments": () => ({ body: paged([APPT], 1, 25, 1) }),
  [`GET /care-gaps/${CASE_ID}/completion-evidence`]: () => ({ body: ok(EVIDENCE) }),
  "GET /analytics/engagement": () => ({
    body: ok({
      simulated: true,
      synthetic_status: "simulated",
      note: "All communications, responses, appointments and outcomes are simulated; no message was sent to a real person.",
      communications: { total: 12, by_delivery_state: { queued: 1, sent: 2, delivered: 8, failed: 1 }, by_channel: { sms: 7, whatsapp: 3, phone_call: 1, email: 1 }, by_language: { en: 6, hi: 3, ar: 3 } },
      responses: { total: 6, by_type: { accept: 4, decline: 1, question: 1, no_response: 0 } },
      appointments: { total: 4, by_status: { proposed: 1, simulated_confirmed: 2, completed: 1, no_show: 0, cancelled: 0 } },
      completion_evidence: { total: 2, by_verification_status: { pending: 1, verified: 1, rejected: 0 } },
      cases: { by_status: { planned: 3, completed: 1 } },
      funnel: { cases_contacted: 12, cases_delivered: 8, cases_accepted: 4, cases_with_appointment: 3, cases_attended: 1, cases_with_verified_evidence: 1, cases_completed: 1 },
    }),
  }),
  "GET /analytics/evaluation": () => ({
    body: ok({
      run_id: "20261108T101500Z-1a2b3c4d",
      created_at: "2026-11-08T10:15:00Z",
      run_type: "dev",
      status: "complete",
      synthetic_banner: "All data is synthetic or simulated. Not clinical validation.",
      code: { git_commit: "abcdef1234567890", dirty: false },
      dataset: { dataset_version: "syn-20261105-s42" },
      ground_truth: { ground_truth_id: "gt-1", frozen: true },
      coverage: [
        { tier: "synthetic_population", target: 1000000, actual_completed: 1200 },
        { tier: "ai_evidence_cohort", target: 100000, actual_completed: 0 },
        { tier: "intensive_evaluation", target: 10000, actual_completed: 0 },
      ],
      metrics: {
        "EX-01": { view: "A", value: 0.9, numerator: 90, denominator: 100, n: 100, unit: "ratio", ci95: [0.82, 0.95], status: "measured", flags: [] },
        "EX-02": { view: "A", value: null, unit: "ratio", status: "not_applicable", flags: ["low_n"] },
      },
      gates: [{ gate_id: "SG-02", metric_id: "EX-01", comparator: ">=", threshold: 0.95, threshold_status: "proposal", observed: 0.9, result: "fail" }],
      safety_cases: [{ metric_id: "SAF-01", case_ref: "doc-17" }],
      projections: [{ name: "Cost for 100k residents", formula: "per-document cost x 100000", value: 1234.5, label: "projected" }],
      limitations: ["Synthetic labels were generated by the same team that built the pipeline."],
    }),
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
        { id: "c1", clinic_key: "north-1", name: "North Clinic", district_code: "D03", address: null, timezone: "UTC", operating_status: "open", synthetic_status: "simulated", services: [{ service_key: "mammography", service_name: "Mammography", appointment_duration_minutes: 30, active: true }] },
        { id: "c2", clinic_key: "south-1", name: "South Clinic", district_code: "D07", address: null, timezone: "UTC", operating_status: "closed", synthetic_status: "simulated", services: [] },
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
