import { commandCenter } from "./features-fixtures-ref";
import { MODULE_HANDLERS } from "./fixtures";
import { funnelPayload } from "./features-fixtures";
import { ok, type ApiHandler } from "./helpers";

// Route mocks for Population, Insights and Reports: the comparison block, explorer, scope totals and programme coverage.
// Invented values for tests only. Shapes follow backend/anticipa/analytics/{service,geography}.py.

// The ten pilot districts of the shared geography mock (fixtures.ts), so the map matches the districts returned here.
const D = [
  ["D01", "Bhopal", "23", "Madhya Pradesh", 21480], ["D02", "Indore", "23", "Madhya Pradesh", 19820], ["D03", "Jabalpur", "23", "Madhya Pradesh", 20410],
  ["D04", "Gwalior", "23", "Madhya Pradesh", 18950], ["D05", "Pune", "27", "Maharashtra", 22140], ["D06", "Nagpur", "27", "Maharashtra", 19330],
  ["D07", "Lucknow", "09", "Uttar Pradesh", 20870], ["D08", "Varanasi", "09", "Uttar Pradesh", 18240], ["D09", "Bengaluru Urban", "29", "Karnataka", 19790],
  ["D10", "Mysuru", "29", "Karnataka", 18970],
] as const;
const dueOf = (r: number) => Math.round(r * 0.12);
/** Residents due across the ten districts, as the Equity card totals them. */
export const DUE_TOTAL = D.reduce((s, d) => s + dueOf(d[4]), 0);

const BANDS = ["18-29", "30-39", "40-49", "50-64", "65-79", "80+"];
const MALE = [3000, 2800, 2600, 2900, 1800, 600];
const FEMALE = [3200, 3000, 2900, 3100, 2200, 900];

export function explorerPayload(url: URL) {
  const district = url.searchParams.get("district");
  const band = url.searchParams.get("age_band");
  const sex = url.searchParams.get("sex");
  const k = district ? 0.1 : 1;
  const cells = BANDS.flatMap((b, i) => [
    { age_band: b, sex: "male", residents: Math.round(MALE[i] * k), open_care_gaps: Math.round(MALE[i] * k * 0.2), due_residents: Math.round(MALE[i] * k * 0.12) },
    { age_band: b, sex: "female", residents: Math.round(FEMALE[i] * k), open_care_gaps: Math.round(FEMALE[i] * k * 0.2), due_residents: Math.round(FEMALE[i] * k * 0.12) },
  ]);
  const inSlice = cells.filter((c) => (!band || c.age_band === band) && (!sex || c.sex === sex));
  const sum = (f: (c: (typeof cells)[number]) => number) => inSlice.reduce((s, c) => s + f(c), 0);
  const population = sum((c) => c.residents);
  const due = sum((c) => c.due_residents);
  const open = sum((c) => c.open_care_gaps);
  return {
    available: true,
    filters: { district, age_band: band, sex },
    options: {
      districts: D.map((d) => ({ code: d[0], name: d[1], state: d[3] })),
      age_bands: BANDS,
      sexes: ["female", "male"],
    },
    slice: { population, due_residents: due, open_care_gaps: open, cases_total: open + 40, completed: 40, in_outreach: 25, gaps_per_100: population ? Math.round((open / population) * 1000) / 10 : null, closure_rate: 0.0346, coverage: 0.48 },
    pyramid: cells,
    definitions: {
      due_residents: { label: "Due for care", definition: "Residents whose latest evaluation is eligible or needs clinical review." },
      open_care_gaps: { label: "Open care gaps", definition: "Care gap cases that are not completed or closed." },
      gaps_per_100: { label: "Care gaps per 100 residents", definition: "Open care gaps over residents, times 100." },
      closure_rate: { label: "Closure rate", definition: "Completed cases over all cases." },
      coverage: { label: "Planning coverage", definition: "Cases with a plan over all cases." },
    },
  };
}

export function commandCenterPop(previous = 0) {
  const base = commandCenter();
  const districts = D.map((d, i) => {
    const cases = Math.round(d[4] * 0.13);
    const planned = Math.round(cases * (0.42 + (i % 5) * 0.03));
    const completed = Math.round(cases * (0.02 + (i % 4) * 0.01));
    return { district_code: d[0], residents: d[4], due_residents: dueOf(d[4]), due_share: dueOf(d[4]) / d[4], cases, planned, completed, coverage: planned / cases, closure_rate: completed / cases };
  });
  const cov = [...districts].sort((x, y) => x.coverage - y.coverage);
  const clo = [...districts].sort((x, y) => x.closure_rate - y.closure_rate);
  const spread = (a: typeof cov, k: "coverage" | "closure_rate") => ({ min: { district_code: a[0].district_code, value: a[0][k] }, max: { district_code: a[a.length - 1].district_code, value: a[a.length - 1][k] }, gap: a[a.length - 1][k] - a[0][k] });
  return {
    ...base,
    equity: { ...base.equity, districts, coverage: spread(cov, "coverage"), closure_rate: spread(clo, "closure_rate") },
    comparison: {
      available: true,
      window_days: 30,
      kpis: {
        care_gaps_open: { current: 23000, previous, delta: previous ? 23000 - previous : 23000, delta_pp: null },
        gaps_closed: { current: 900, previous, delta: previous ? 900 - previous : 900, delta_pp: null },
        closure_rate: { current: 0.0346, previous: previous ? 0.03 : 0, delta: previous ? 0.0046 : 0.0346, delta_pp: previous ? 0.5 : 3.5 },
      },
      flows: { closed: { current: 900, previous, delta: 900 - previous, delta_pct: previous ? ((900 - previous) / previous) * 100 : null }, opened: { current: 26000, previous, delta: 26000 - previous, delta_pct: null } },
    },
  };
}

const prog = (a: string, label: string, cases: number, upToDate: number, due: number) => ({
  care_activity: a, label, cases_total: cases, open_care_gaps: cases - 40, completed: 40, in_outreach: 25, due_residents: due, closure_rate: 40 / cases,
  coverage: 0.48, up_to_date_residents: upToDate, screening_coverage: upToDate / (upToDate + due),
});
export const programmesCoverage = () => ({
  generated_at: "2026-11-12T08:00:00Z",
  population: 200000,
  programmes: [prog("breast_cancer_screening", "Breast cancer screening", 13000, 30000, 12000), prog("cervical_cancer_screening", "Cervical cancer screening", 6500, 21000, 6000), prog("colorectal_cancer_screening", "Colorectal cancer screening", 4000, 9000, 4500)],
});

export const scopeFull = () => ({
  generated_at: "2026-11-12T08:00:00Z",
  country: { code: "IN", name: "India" },
  states: [["23", "IN-MP", "Madhya Pradesh"], ["27", "IN-MH", "Maharashtra"], ["09", "IN-UP", "Uttar Pradesh"], ["29", "IN-KA", "Karnataka"]].map(([code, iso, name]) => ({
    code, name, iso_code: iso, type: "state", district_codes: D.filter((d) => d[2] === code).map((d) => d[0]),
  })),
  districts: D.map((d, i) => ({ code: d[0], name: d[1], state_code: d[2], state: d[3], lat: 20 + i, lon: 75 + i, facilities: i % 2 ? 1 : 2 })),
  totals: { countries: 1, states_with_coverage: 4, states_listed: 36, districts: 10, facilities: 15 },
  facilities_source: "simulated",
  not_available: { households_total: "Residents carry no household attribute." },
});

/** Handlers for the three pages. `previous` is the earlier period value (0 means no earlier period yet). */
export const popHandlers = (previous = 0): Record<string, ApiHandler> => ({
  ...MODULE_HANDLERS,
  "GET /analytics/command-center": () => ({ body: ok(commandCenterPop(previous)) }),
  "GET /analytics/programmes": () => ({ body: ok(programmesCoverage()) }),
  "GET /meta/scope": () => ({ body: ok(scopeFull()) }),
  "GET /analytics/population-explorer": (_r: unknown, url: URL) => ({ body: ok(explorerPayload(url)) }),
  "GET /analytics/funnel": () => ({ body: ok(funnelPayload()) }),
  // The real overview keeps storage wording in its labels and definitions; the page maps those to product wording.
  "GET /analytics/population-overview": async (r, url) => {
    const res = await MODULE_HANDLERS["GET /analytics/population-overview"](r, url);
    const m = (key: string, label: string, value: number, definition: string) => ({ key, label, value, definition });
    (res.body as { data: { metrics: unknown[] } }).data.metrics = [
      m("residents_total", "Residents", 200000, "Rows in anticipa.residents for this dataset (synthetic residents; one row per Synthea patient)."),
      m("residents_deceased", "Residents with a recorded death date", 0, "Residents whose deceased_date is set."),
      m("residents_full_profile", "Residents with full clinical detail", 190000, "Residents loaded under the `full` storage profile."),
      m("residents_rules_profile", "Residents with rule-relevant detail only", 10000, "Residents loaded under the `rules` storage profile."),
      m("residents_with_family_links", "Residents with at least one family link", 150000, "Distinct residents with >= 1 row in anticipa.family_edges."),
      m("family_edges_total", "Family edges (directed rows)", 600000, "Rows in anticipa.family_edges."),
      m("family_units", "Constructed family units", 48000, "Family units recorded by the linker run."),
      m("documents_total", "Registered documents", 32523, "Rows in anticipa.documents (synthetic documents)."),
    ];
    return res;
  },
  "GET /analytics/geography": async (r, url) => {
    const res = await MODULE_HANDLERS["GET /analytics/geography"](r, url);
    const data = (res.body as { data: { level: string; area: { programmes: unknown[] } } }).data;
    data.area.programmes = programmesCoverage().programmes.slice(0, data.level === "district" ? 1 : 3);
    return res;
  },
});
