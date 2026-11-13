// Route-mock fixtures for the reference pages (journey, programs, impact, operations, planning, evidence). Invented demo
// values for tests only: they are not measurements. Shapes mirror backend/anticipa/analytics/service.py and the planning,
// engagement and caregaps routes (read 2026-11-12).

export const commandCenter = () => ({
  generated_at: "2026-11-12T08:00:00Z",
  cache_ttl_seconds: 15,
  limited: false,
  restricted: [],
  dataset: { latest_version: "syn-20261101-s1" },
  kpis: [
    { key: "residents_total", label: "Residents", value: 200000, definition: "x", source: "synthetic" },
    { key: "residents_with_open_gaps", label: "Residents with open care gaps", value: 21000, definition: "x", source: "synthetic" },
    { key: "care_gaps_open", label: "Open care gaps", value: 23000, definition: "x", source: "synthetic" },
    { key: "care_gaps_total", label: "Care gaps opened", value: 26000, definition: "x", source: "synthetic" },
    { key: "gaps_closed", label: "Gaps closed", value: 900, definition: "x", source: "simulated" },
    { key: "closure_rate", label: "Closure rate", value: 0.0346, definition: "x", source: "simulated", numerator: 900, denominator: 26000 },
    { key: "in_outreach", label: "Currently in outreach", value: 5100, definition: "x", source: "simulated" },
  ],
  gaps_by_priority: {
    definition: "x",
    items: [
      { priority: "high", open: 4000, completed: 300, total: 4400 },
      { priority: "elevated", open: 9000, completed: 400, total: 9500 },
      { priority: "routine", open: 10000, completed: 200, total: 12100 },
    ],
  },
  closure_trend: { days: 30, points: [{ date: "2026-11-10", closed: 12, opened: 400 }, { date: "2026-11-11", closed: 30, opened: 650 }] },
  equity: {
    definition: "x",
    districts: [
      { district_code: "PUN", residents: 40000, cases: 6000, planned: 3000, completed: 300, coverage: 0.5, closure_rate: 0.05 },
      { district_code: "MUM", residents: 60000, cases: 9000, planned: 4000, completed: 200, coverage: 0.44, closure_rate: 0.022 },
      { district_code: "NAG", residents: 30000, cases: 4000, planned: 1800, completed: 180, coverage: 0.45, closure_rate: 0.045 },
    ],
    coverage: { min: { district_code: "MUM", value: 0.44 }, max: { district_code: "PUN", value: 0.5 }, gap: 0.06 },
    closure_rate: { min: { district_code: "MUM", value: 0.022 }, max: { district_code: "PUN", value: 0.05 }, gap: 0.028 },
  },
  recent_activity: { definition: "x", items: [] },
  alert_rules: [
    { key: "capacity", title: "Capacity nearly full", status: "alert", severity: "warning", value: 0.93, threshold: 0.9, message: "Booked seats are above 90 percent in the next 7 days.", definition: "x" },
    { key: "review_queue", title: "Evidence review queue", status: "ok", severity: null, value: 0.02, threshold: 0.1, message: "Within the limit.", definition: "x" },
  ],
  alerts: [{ key: "capacity", title: "Capacity nearly full", status: "alert", severity: "warning", value: 0.93, threshold: 0.9, message: "Booked seats are above 90 percent in the next 7 days.", definition: "x" }],
});

export const engagementOverview = () => ({
  simulated: true,
  note: "x",
  communications: { total: 6100, by_delivery_state: { delivered: 5000, failed: 100 }, by_channel: { sms: 4000, phone: 2100 }, by_language: { en: 3000, hi: 2000, ar: 1100 } },
  responses: { total: 3000, by_type: { accepted: 2000 } },
  appointments: { total: 2500, by_status: { completed: 1500, simulated_confirmed: 800, no_show: 200 } },
  completion_evidence: { total: 900, by_verification_status: { verified: 900 } },
  cases: { by_status: {} },
  funnel: { cases_contacted: 6100, cases_delivered: 5000, cases_accepted: 2000, cases_with_appointment: 2500, cases_attended: 1500, cases_with_verified_evidence: 900, cases_completed: 900 },
});

export const careGapRows = (n = 3) =>
  Array.from({ length: n }, (_, i) => ({
    id: `eeeeeeee-0000-4000-8000-0000000001${String(i).padStart(2, "0")}`,
    resident_id: `cccccccc-0000-4000-8000-0000000001${String(i).padStart(2, "0")}`,
    district_code: "PUN",
    evaluation_id: "dddddddd-0000-4000-8000-000000000001",
    guideline_version_id: "dddddddd-0000-4000-8000-0000000000a2",
    rule_version_id: "dddddddd-0000-4000-8000-0000000000a3",
    care_activity: ["breast_cancer_screening", "cervical_cancer_screening", "colorectal_cancer_screening"][i % 3],
    status: ["planned", "outreach_pending", "completed"][i % 3],
    priority: ["high", "elevated", "routine"][i % 3],
    source_outcome: "eligible",
    assigned_coordinator_id: null,
    planning_run_id: null,
    opened_at: "2026-11-11T09:00:00Z",
    updated_at: "2026-11-11T10:00:00Z",
    closed_at: null,
    closure_reason: null,
    synthetic_status: "synthetic",
  }));
