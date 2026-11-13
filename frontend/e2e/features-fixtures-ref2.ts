import { careGapRows } from "./features-fixtures-ref";

// More route-mock fixtures for the reference pages: capacity, guidelines and the care gap stage counts. Invented demo
// values for tests only.

export const capacityOverview = () => ({
  simulated: true,
  window: { start: "2026-11-12T00:00:00Z", end: "2026-11-19T00:00:00Z", source: "default" },
  capacity: { total_seats: 10000, reserved_seats: 8000, remaining_seats: 2000, utilization: 0.8, note: "Seats, not appointments." },
  by_district: [
    { district_code: "PUN", total_seats: 4000, reserved_seats: 3800, remaining_seats: 200, utilization: 0.95, planned_seats: 3000 },
    { district_code: "MUM", total_seats: 4000, reserved_seats: 2800, remaining_seats: 1200, utilization: 0.7, planned_seats: 2500 },
    { district_code: "NAG", total_seats: 2000, reserved_seats: 1400, remaining_seats: 600, utilization: 0.7, planned_seats: 1000 },
  ],
  latest_run: { id: "ffffffff-0000-4000-8000-0000000000b1", completed_at: "2026-11-12T07:00:00Z", cases_total: 9000, allocated: 7000, unallocated: 2000, coverage: 0.78, plan_utilization: 0.7, reason_counts: { no_capacity: 1500, language_unsupported: 500 } },
});

const version = (id: string, gid: string, activity: string, priority: string) => ({
  id, guideline_id: gid, version: 1, review_status: "approved", care_activity: activity, priority, source_reference: null, source_publication_date: "2024-04-30", effective_from: "2026-01-01", effective_until: null,
  population_scope: {}, disclaimer: null, change_summary: null, source_version: null, clinical_review_status: "approved", content_hash: null, created_at: "2026-11-01T00:00:00Z", submitted_at: null,
  approved_by: null, approved_at: "2026-11-02T00:00:00Z", retired_at: null, retirement_reason: null,
});

export const guidelinesList = () => [
  { id: "dddddddd-0000-4000-8000-000000000001", guideline_key: "breast_cancer_screening", name: "Breast cancer screening", description: null, topic: "cancer_screening", authoritative_source: "Example Task Force", source_url: null, created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-01T00:00:00Z", versions: [version("v1", "dddddddd-0000-4000-8000-000000000001", "breast_cancer_screening", "high")] },
  { id: "dddddddd-0000-4000-8000-000000000002", guideline_key: "cervical_cancer_screening", name: "Cervical cancer screening", description: null, topic: "cancer_screening", authoritative_source: "Example Task Force", source_url: null, created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-01T00:00:00Z", versions: [version("v2", "dddddddd-0000-4000-8000-000000000002", "cervical_cancer_screening", "elevated")] },
];

/** GET /care-gaps answer sized by the stage the query asks for (page_size=1 count calls); half as many for one programme. */
export function careGapTotals(url: URL) {
  const status = url.searchParams.getAll("status");
  const scale = url.searchParams.get("care_activity") ? 0.5 : 1;
  const has = (s: string) => status.includes(s);
  const n = status.length === 0 ? 26000 : has("planned") ? 12500 : has("outreach_pending") ? 6100 : has("awaiting_completion_evidence") ? 1500 : has("completed") ? 900 : 0;
  return { total: Math.round(n * scale), rows: careGapRows(Math.min(Number(url.searchParams.get("page_size") ?? 15), 3)) };
}

export const programmesPayload = () => ({
  generated_at: "2026-11-12T08:00:00Z",
  population: 200000,
  programmes: [
    { care_activity: "breast_cancer_screening", label: "Breast cancer screening", cases_total: 13000, open_care_gaps: 7000, completed: 450, in_outreach: 3000, due_residents: 12000, closure_rate: 0.0346, coverage: 0.48 },
    { care_activity: "cervical_cancer_screening", label: "Cervical cancer screening", cases_total: 6500, open_care_gaps: 3500, completed: 225, in_outreach: 1500, due_residents: 6000, closure_rate: 0.0346, coverage: 0.48 },
  ],
});

const prog = (a: string, label: string, cases: number, coverage: number, closure: number) => ({ care_activity: a, label, cases_total: cases, open_care_gaps: cases - 10, completed: 10, in_outreach: 5, due_residents: cases, closure_rate: closure, coverage });
const area = (level: string, code: string, name: string, cases: number, coverage: number | null, closure: number | null, extra: Record<string, unknown> = {}) => ({
  level, code, name, population: cases * 4, due_residents: cases, open_care_gaps: cases - 10, cases_total: cases, completed: 10, in_outreach: 5, closure_rate: closure, coverage,
  programmes: [prog("breast_cancer_screening", "Breast cancer screening", Math.round(cases * 0.6), (coverage ?? 0) + 0.05, closure ?? 0), prog("cervical_cancer_screening", "Cervical cancer screening", Math.round(cases * 0.4), (coverage ?? 0) - 0.05, closure ?? 0)], ...extra,
});
export const geographyCountry = () => ({
  level: "country", code: null, area: area("country", "IN", "India", 15000, 0.48, 0.03),
  areas: [
    area("state", "27", "Maharashtra", 10000, 0.5, 0.04, { coverage_status: "pilot", district_codes: ["PUN", "MUM"] }),
    area("state", "29", "Karnataka", 5000, 0.4, 0.02, { coverage_status: "pilot", district_codes: ["NAG"] }),
    area("state", "01", "Goa", 0, null, null, { coverage_status: "no_coverage" }),
  ],
});
export const geographyState = (code: string) => ({
  level: "state", code, area: area("state", code, code === "27" ? "Maharashtra" : "Karnataka", 1, 0.4, 0.03),
  areas: code === "27" ? [area("district", "PUN", "Pune", 6000, 0.55, 0.05), area("district", "MUM", "Mumbai", 4000, 0.42, 0.02)] : [area("district", "NAG", "Nagpur", 5000, 0.4, 0.02)],
});
