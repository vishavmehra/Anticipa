// Volume-realistic API payloads (200,000 residents, 10 districts) used only for the reference-comparison screenshots.
// Shapes follow the live endpoints; the UI renders whatever these return, nothing is hardcoded in the pages.
import { ok, type ApiHandler } from "./helpers";
import { MODULE_HANDLERS } from "./fixtures";

const DIST = ["D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10"];
const residents = [21480, 19820, 20410, 18950, 22140, 19330, 20870, 18240, 19790, 18970];
const cases = [11820, 9210, 13440, 10980, 14210, 9980, 12150, 8890, 11030, 9940];
const planned = [9100, 7300, 9800, 8500, 10200, 7700, 9900, 6900, 8700, 7600];
const completed = [1430, 980, 1510, 1260, 1620, 1010, 1700, 840, 1290, 1060];

const days = 30;
const trend = Array.from({ length: days }, (_, i) => {
  const d = new Date(Date.UTC(2026, 9, 14 + i));
  const closed = Math.round(380 + 160 * Math.sin(i / 4) + i * 7);
  return { date: d.toISOString().slice(0, 10), closed, opened: closed + 120 + Math.round(60 * Math.cos(i / 3)) };
});

export const REF_HANDLERS: Record<string, ApiHandler> = {
  ...MODULE_HANDLERS,
  "GET /analytics/command-center": () => ({
    body: ok({
      generated_at: "2026-11-12T18:30:00Z",
      cache_ttl_seconds: 15,
      limited: false,
      restricted: [],
      dataset: { latest_version: "syn-20261108-s42" },
      kpis: [
        { key: "residents_total", label: "Residents", value: 200000, definition: "Rows in residents.", source: "live" },
        { key: "residents_with_open_gaps", label: "Residents with open care gaps", value: 83412, definition: "Distinct residents with an open case.", source: "live" },
        { key: "care_gaps_open", label: "Open care gaps", value: 116411, definition: "Open cases.", source: "live" },
        { key: "care_gaps_total", label: "Care gaps opened", value: 131204, definition: "All cases.", source: "live" },
        { key: "gaps_closed", label: "Gaps closed", value: 14793, definition: "Completed cases.", source: "live" },
        { key: "closure_rate", label: "Closure rate", value: 0.1128, definition: "Completed / all cases.", source: "live", numerator: 14793, denominator: 131204 },
        { key: "in_outreach", label: "Currently in outreach", value: 9120, definition: "Outreach statuses.", source: "live" },
      ],
      gaps_by_priority: {
        definition: "Cases by priority.",
        source: "live",
        items: [
          { priority: "high", open: 39612, completed: 3010, total: 42622 },
          { priority: "elevated", open: 52480, completed: 6420, total: 58900 },
          { priority: "routine", open: 24319, completed: 5363, total: 29682 },
        ],
      },
      closure_trend: { definition: "Per UTC day.", source: "live", days, points: trend },
      comparison: { available: true, window_days: 30, kpis: { care_gaps_open: { current: 116411, previous: 104200, delta: 12211, delta_pp: null }, closure_rate: { current: 0.1128, previous: 0.0912, delta: 0.0216, delta_pp: 2.2 } } },
      equity: {
        definition: "Per district.",
        source: "live",
        districts: DIST.map((c, i) => ({ district_code: c, residents: residents[i], cases: cases[i], planned: planned[i], completed: completed[i], coverage: planned[i] / cases[i], closure_rate: completed[i] / cases[i] })),
        coverage: { min: { district_code: "D08", value: 6900 / 8890 }, max: { district_code: "D05", value: 10200 / 14210 }, gap: 0.0 },
        closure_rate: { min: { district_code: "D04", value: 0.1147 }, max: { district_code: "D07", value: 0.1399 }, gap: 0.0252 },
      },
      recent_activity: null,
      alert_rules: [
        { key: "capacity_utilisation", title: "Clinic capacity nearly full", status: "alert", severity: "warning", value: 0.93, threshold: 0.9, message: "93% of seats in the next 7 days are reserved.", definition: "Reserved seats / available seats.", source: "live" },
        { key: "evidence_review_share", title: "Evidence awaiting review", status: "ok", severity: null, value: 0.18, threshold: 0.5, message: "18% of extracted facts await review.", definition: "Pending / all facts.", source: "live" },
      ],
      alerts: [{ key: "capacity_utilisation", title: "Clinic capacity nearly full", status: "alert", severity: "warning", value: 0.93, threshold: 0.9, message: "93% of seats in the next 7 days are reserved.", definition: "Reserved seats / available seats.", source: "live" }],
    }),
  }),
  "GET /analytics/funnel": () => ({
    body: ok({
      generated_at: "2026-11-12T18:30:00Z",
      cache_ttl_seconds: 15,
      synthetic_banner: "",
      sources: {},
      note: "",
      stages: [
        ["residents", "Residents", 200000],
        ["evaluated", "Evaluated", 198400],
        ["due", "Due for care", 83412],
        ["case_opened", "Case opened", 83412],
        ["planned", "Planned", 52100],
        ["in_outreach", "In outreach", 9120],
        ["closed", "Closed", 14793],
      ].map(([key, label, count], i, a) => ({
        key,
        label,
        count,
        cases: null,
        unit: "residents",
        share_of_residents: (count as number) / 200000,
        conversion_from_previous: i ? (count as number) / (a[i - 1][2] as number) : null,
        definition: "Distinct residents.",
        source: "live",
      })),
      leakage: { definition: "Cases that left the funnel.", source: "live", items: [] },
    }),
  }),
  "GET /analytics/population-overview": () => ({
    body: ok({
      dataset_version: "syn-20261108-s42",
      generator: "synthea 3.3.0",
      reference_date: "2026-11-08",
      metrics: [
        { key: "residents_total", label: "Residents", value: 200000, definition: "Rows in residents for this dataset." },
        { key: "residents_with_family_links", label: "Residents with at least one family link", value: 151220, definition: "Distinct residents with at least one family link." },
        { key: "documents_total", label: "Registered documents", value: 98400, definition: "Rows in documents for this dataset's residents." },
      ],
      distributions: [
        {
          key: "age_band",
          label: "Age band",
          definition: "Completed years on the dataset reference date, from birth date.",
          buckets: [["0-17", 38400], ["18-29", 32600], ["30-39", 31200], ["40-49", 29800], ["50-59", 27100], ["60-69", 22400], ["70-79", 12600], ["80+", 5900]].map(([key, count]) => ({ key, count, share: (count as number) / 200000 })),
        },
        { key: "sex", label: "Administrative sex", definition: "Administrative sex as generated.", buckets: [{ key: "female", count: 101200, share: 0.506 }, { key: "male", count: 98800, share: 0.494 }] },
        { key: "language", label: "Preferred language", definition: "Preferred language recorded for the resident.", buckets: [{ key: "en", count: 92000, share: 0.46 }, { key: "hi", count: 70000, share: 0.35 }, { key: "ar", count: 38000, share: 0.19 }] },
        { key: "district", label: "District", definition: "District code of residence.", buckets: DIST.map((k, i) => ({ key: k, count: residents[i], share: residents[i] / 200000 })) },
      ],
      basis: { residents: "live count of residents" },
      profiles: [],
      storage_policy: null,
      limited: false,
      notice: "",
    }),
  }),
};
