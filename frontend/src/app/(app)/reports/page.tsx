"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { displayText } from "@/lib/display";
import { ageBandLabel, fmtDate, fmtDateTime, fmtDayMonth, fmtInt, fmtPct, humanize } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import type { CommandCenter, GeoResponse, PopulationOverview, ScopeMeta } from "@/lib/types";
import type { Funnel, PopulationExplorer, ProgrammeRow } from "@/components/features/payloads";
import { useProgrammes } from "@/components/features/kit";
import { RANGES, useRange } from "@/components/app-shell";
import {
  AreaTrend,
  BarList,
  Card,
  CardHeader,
  EmptyState,
  ErrorPanel,
  FilterField,
  Icon,
  PageHeader,
  PillTabs,
  Pill,
  btnPrimary,
  btnSecondary,
  selectClass,
  type IconName,
} from "@/components/ui";

type Tab = "generate" | "saved" | "recent";
const TABS = [
  { id: "generate", label: "Generate report", icon: "file" },
  { id: "saved", label: "Saved reports", icon: "bookmark" },
  { id: "recent", label: "Recent reports", icon: "clock" },
] as const satisfies readonly { id: Tab; label: string; icon: IconName }[];

type Section = "population" | "journey" | "trends" | "equity" | "programmes";
const SECTIONS: { id: Section; label: string; icon: IconName }[] = [
  { id: "population", label: "Population overview", icon: "users-round" },
  { id: "programmes", label: "Programme performance", icon: "shield" },
  { id: "journey", label: "Care journey performance", icon: "journey" },
  { id: "trends", label: "Coverage and trends", icon: "chart" },
  { id: "equity", label: "Equity and access", icon: "scale" },
];
type ReportType = "population" | "programme" | "district" | "caregap" | "equity" | "operations";
const TYPES: Record<ReportType, { label: string; title: string; subtitle: string; sections: Section[] }> = {
  population: { label: "Population summary", title: "Population Summary Report", subtitle: "Who is in the population and where care is needed", sections: ["population", "journey"] },
  programme: { label: "Programme performance", title: "Programme Performance Report", subtitle: "Reach, coverage and closure for each programme", sections: ["programmes", "trends"] },
  district: { label: "District comparison", title: "District Comparison Report", subtitle: "Coverage, closure and need across districts", sections: ["equity", "trends"] },
  caregap: { label: "Care gap analysis", title: "Care Gap Analysis Report", subtitle: "Open care gaps, priority and progress", sections: ["journey", "trends"] },
  equity: { label: "Equity and access", title: "Equity and Access Report", subtitle: "Whether planning reaches every district equally", sections: ["equity"] },
  operations: { label: "Monthly operations", title: "Monthly Operations Report", subtitle: "Cases opened, closed and in progress", sections: ["trends", "journey"] },
};
const QUICK: ReportType[] = ["population", "programme", "district", "caregap", "operations"];

/** What a report is made of. Saving, sharing and the recent list all store this, never the figures. */
interface Def {
  type: ReportType;
  district: string;
  programme: string;
  days: number;
  sections: Section[];
}
const SAVED_KEY = "anticipa-saved-reports-v1";
const RECENT_KEY = "anticipa-recent-reports-v1";
interface Stored extends Def {
  id: string;
  at: string;
}

function readList(key: string): Stored[] {
  try {
    const v = JSON.parse(window.localStorage.getItem(key) ?? "[]");
    return Array.isArray(v) ? (v as Stored[]).filter((x) => x && x.type in TYPES) : [];
  } catch {
    return [];
  }
}
function writeList(key: string, list: Stored[]) {
  try {
    window.localStorage.setItem(key, JSON.stringify(list));
  } catch {
    /* storage can be blocked; the page still works without it */
  }
}
const entry = (d: Def): Stored => ({ ...d, id: `${Date.now()}`, at: new Date().toISOString() });
const sameDef = (a: Def, b: Def) => a.type === b.type && a.district === b.district && a.programme === b.programme && a.days === b.days && a.sections.join() === b.sections.join();
const defQuery = (d: Def) => new URLSearchParams({ type: d.type, district: d.district, programme: d.programme, days: String(d.days), sections: d.sections.join(",") }).toString();

function fromParams(p: URLSearchParams, fallbackDays: number): Def {
  const type = (p.get("type") ?? "population") as ReportType;
  const t: ReportType = type in TYPES ? type : "population";
  const sec = (p.get("sections") ?? "").split(",").filter((s): s is Section => SECTIONS.some((x) => x.id === s));
  const days = Number(p.get("days"));
  return { type: t, district: p.get("district") ?? "", programme: p.get("programme") ?? "", days: RANGES.some((r) => r.days === days) ? days : fallbackDays, sections: sec.length ? sec : TYPES[t].sections };
}

// A spreadsheet treats a cell that starts with = + - @ (or a tab / carriage return, which some programs skip before the formula
// check) as a formula, so text from the server is defused before it is written. Numbers are written as they are.
const csvCell = (v: string | number | null | undefined) => {
  const s = String(v ?? "");
  return `"${(typeof v === "string" && /^[\s=+\-@]/.test(s) ? `'${s}` : s).replace(/"/g, '""')}"`;
};

/** Age band and sex totals for the chosen district, from the explorer pyramid (the overview endpoint has no district filter). */
function demographics(ex: PopulationExplorer | null | undefined, pop: PopulationOverview | null | undefined) {
  if (ex?.available && ex.pyramid.length) {
    const order = [...ex.options.age_bands, ...new Set(ex.pyramid.map((c) => c.age_band).filter((b) => !ex.options.age_bands.includes(b)))];
    const total = ex.pyramid.reduce((s, c) => s + c.residents, 0) || 1;
    const age = order.map((b) => ({ key: b, count: ex.pyramid.filter((c) => c.age_band === b).reduce((s, c) => s + c.residents, 0) }));
    const sexes = [...new Set(ex.pyramid.map((c) => c.sex))];
    const sex = sexes.map((k) => {
      const count = ex.pyramid.filter((c) => c.sex === k).reduce((s, c) => s + c.residents, 0);
      return { key: k, count, share: count / total };
    });
    return { age, sex };
  }
  const age = pop?.distributions.find((d) => d.key === "age_band")?.buckets;
  const sex = pop?.distributions.find((d) => d.key === "sex")?.buckets;
  return age && sex ? { age, sex } : null;
}

interface Built {
  at: string;
  def: Def;
  cc: CommandCenter | null;
  pop: PopulationOverview | null;
  funnel: Funnel | null;
  ex: PopulationExplorer | null;
  programmes: ProgrammeRow[] | null;
}

function download(b: Built) {
  const dists = (b.cc?.equity?.districts ?? []).filter((d) => !b.def.district || d.district_code === b.def.district);
  const progs = (b.programmes ?? []).filter((p) => !b.def.programme || p.care_activity === b.def.programme);
  const demo = demographics(b.ex, b.pop);
  const s = b.def.sections;
  const rows: (string | number | null)[][] = [["Report", TYPES[b.def.type].title], ["Generated", b.at], ["Window days", b.def.days], ["District", b.def.district || "All districts"], []];
  for (const k of b.cc?.kpis ?? []) rows.push([k.label, k.value]);
  if (s.includes("population") && demo) {
    rows.push([], ["Age band", "Residents"]);
    for (const a of demo.age) rows.push([ageBandLabel(a.key), a.count]);
    rows.push([], ["Sex", "Residents"]);
    for (const x of demo.sex) rows.push([humanize(x.key), x.count]);
  }
  if (s.includes("programmes") && progs.length) {
    rows.push([], ["Programme", "Cases", "Open care gaps", "Completed", "In outreach", "Residents due", "Residents up to date", "Screening coverage", "Closure rate"]);
    for (const p of progs) rows.push([displayText(p.label), p.cases_total, p.open_care_gaps, p.completed, p.in_outreach, p.due_residents, p.up_to_date_residents, p.screening_coverage, p.closure_rate]);
  }
  if (s.includes("journey") && b.funnel) {
    rows.push([], ["Care journey stage", "Count", "Share of residents"]);
    for (const st of b.funnel.stages) rows.push([st.label, st.count, st.share_of_residents ?? null]);
  }
  if (s.includes("trends") && b.cc?.closure_trend.points.length) {
    rows.push([], ["Date", "Cases opened", "Cases closed"]);
    for (const p of b.cc.closure_trend.points) rows.push([p.date, p.opened, p.closed]);
  }
  if (s.includes("equity") || b.def.type === "district") {
    rows.push([], ["District", "Residents", "Residents due", "Cases", "Planned", "Completed", "Coverage", "Closure rate"]);
    for (const d of dists) rows.push([d.district_code, d.residents, d.due_residents ?? null, d.cases, d.planned, d.completed, d.coverage, d.closure_rate]);
  }
  const blob = new Blob([rows.map((r) => r.map(csvCell).join(",")).join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `anticipa-${b.def.type}-report.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

const H3 = "font-display text-lg font-bold";

function ReportBody({ b }: { b: Built }) {
  const cc = b.cc;
  const dists = (cc?.equity?.districts ?? []).filter((d) => !b.def.district || d.district_code === b.def.district);
  const progs = (b.programmes ?? []).filter((p) => !b.def.programme || p.care_activity === b.def.programme);
  const kp = (key: string) => cc?.kpis.find((k) => k.key === key);
  const pts = cc?.closure_trend.points ?? [];
  const demo = demographics(b.ex, b.pop);
  const sections = b.def.sections;
  const dueTotal = dists.reduce((s, d) => s + (d.due_residents ?? 0), 0);
  return (
    <div className="space-y-6 px-5 pb-6">
      <section aria-labelledby="rp-summary">
        <h3 id="rp-summary" className={H3}>Executive summary</h3>
        <ul className="mt-2 space-y-1.5 text-ink-soft">
          {kp("residents_total") && <li>The population holds {fmtInt(kp("residents_total")?.value)} residents across all districts.</li>}
          {b.def.district && <li>District {b.def.district} is selected. Age, sex, programme, care journey and district tables show that district only.</li>}
          {dueTotal > 0 && <li>{fmtInt(dueTotal)} residents are due for preventive care{b.def.district ? ` in ${b.def.district}` : ""}.</li>}
          {kp("care_gaps_open") && <li>{fmtInt(kp("care_gaps_open")?.value)} care gaps are open across {fmtInt(kp("residents_with_open_gaps")?.value)} residents.</li>}
          {kp("closure_rate")?.value != null && <li>The closure rate is {fmtPct(kp("closure_rate")?.value)}: {fmtInt(kp("gaps_closed")?.value)} cases are completed.</li>}
          {!cc && <li className="text-ink-muted">Command center figures are not available for your role.</li>}
        </ul>
      </section>

      {sections.includes("population") && (
        <section aria-labelledby="rp-pop">
          <h3 id="rp-pop" className={H3}>Population overview</h3>
          {demo ? (
            <div className="mt-2 grid gap-6 md:grid-cols-2 [&>*]:min-w-0">
              <BarList ariaLabel="Report: residents by age band" tableCaption="Report: residents by age band" valueHeader="Residents" labelWidth="w-20" max={Math.max(...demo.age.map((x) => x.count), 1)} rows={demo.age.map((x) => ({ label: ageBandLabel(x.key), value: x.count, display: fmtInt(x.count) }))} />
              <BarList ariaLabel="Report: residents by sex" tableCaption="Report: residents by sex" valueHeader="Residents" labelWidth="w-20" rows={demo.sex.map((x) => ({ label: humanize(x.key), value: x.count, display: fmtInt(x.count), note: `${(x.share * 100).toFixed(1)}%` }))} />
            </div>
          ) : (
            <p className="mt-2 text-ink-muted">Population figures are not available.</p>
          )}
        </section>
      )}

      {sections.includes("programmes") && (
        <section aria-labelledby="rp-programmes">
          <h3 id="rp-programmes" className={H3}>Programme performance</h3>
          {progs.length ? (
            <div className="mt-2 overflow-x-auto" role="region" aria-label="Programme performance table" tabIndex={0}>
              <table className="w-full min-w-[40rem] border-collapse text-left">
                <caption className="sr-only">Report: performance of each programme</caption>
                <thead>
                  <tr className="border-b border-line text-ink-muted">
                    {["Programme", "Cases", "Open", "Completed", "Due", "Up to date", "Screening coverage", "Closure rate"].map((h, i) => (
                      <th key={h} scope="col" className={`py-1.5 font-medium ${i ? "text-right" : ""}`}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {progs.map((p) => (
                    <tr key={p.care_activity} className="border-b border-line last:border-0">
                      <th scope="row" className="py-1.5 font-medium">{displayText(p.label)}</th>
                      <td className="tnum py-1.5 text-right">{fmtInt(p.cases_total)}</td>
                      <td className="tnum py-1.5 text-right">{fmtInt(p.open_care_gaps)}</td>
                      <td className="tnum py-1.5 text-right">{fmtInt(p.completed)}</td>
                      <td className="tnum py-1.5 text-right">{fmtInt(p.due_residents)}</td>
                      <td className="tnum py-1.5 text-right">{fmtInt(p.up_to_date_residents)}</td>
                      <td className="tnum py-1.5 text-right">{fmtPct(p.screening_coverage)}</td>
                      <td className="tnum py-1.5 text-right">{fmtPct(p.closure_rate)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-2 text-xs text-ink-muted">Screening coverage is residents up to date over up to date plus due. Programmes are the care activities of approved guidelines.</p>
            </div>
          ) : (
            <p className="mt-2 text-ink-muted">Programme figures are not available for your role.</p>
          )}
        </section>
      )}

      {sections.includes("journey") && (
        <section aria-labelledby="rp-journey">
          <h3 id="rp-journey" className={H3}>Care journey performance</h3>
          {b.funnel ? (
            <table className="mt-2 w-full border-collapse text-left">
              <caption className="sr-only">Report: residents at each stage of the care journey</caption>
              <thead>
                <tr className="border-b border-line text-ink-muted">
                  <th scope="col" className="py-1.5 font-medium">Stage</th>
                  <th scope="col" className="py-1.5 text-right font-medium">Count</th>
                  <th scope="col" className="py-1.5 text-right font-medium">Share of residents</th>
                </tr>
              </thead>
              <tbody>
                {b.funnel.stages.map((s) => (
                  <tr key={s.key} className="border-b border-line last:border-0">
                    <th scope="row" className="py-1.5 font-medium">{s.label}</th>
                    <td className="tnum py-1.5 text-right">{fmtInt(s.count)}</td>
                    <td className="tnum py-1.5 text-right">{s.share_of_residents == null ? "None" : fmtPct(s.share_of_residents)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="mt-2 text-ink-muted">The care journey is not available for your role.</p>
          )}
        </section>
      )}

      {sections.includes("trends") && (
        <section aria-labelledby="rp-trends">
          <h3 id="rp-trends" className={H3}>Coverage and trends</h3>
          {pts.length ? <AreaTrend points={pts.map((p, i) => ({ label: fmtDayMonth(p.date) || String(i), value: p.closed }))} ariaLabel="Report: cases closed per day" tableCaption="Report: cases closed per day" seriesLabel="Closed" yFormat={(n) => String(Math.round(n))} /> : <p className="mt-2 text-ink-muted">No case activity in this window.</p>}
        </section>
      )}

      {sections.includes("equity") && (
        <section aria-labelledby="rp-equity">
          <h3 id="rp-equity" className={H3}>Equity and access</h3>
          {dists.length ? (
            <div className="mt-2 overflow-x-auto" role="region" aria-label="Equity and access table" tabIndex={0}>
              <table className="w-full min-w-[38rem] border-collapse text-left">
                <caption className="sr-only">Report: coverage and closure by district</caption>
                <thead>
                  <tr className="border-b border-line text-ink-muted">
                    {["District", "Residents", "Due", "Cases", "Planned", "Completed", "Coverage", "Closure rate"].map((h, i) => (
                      <th key={h} scope="col" className={`py-1.5 font-medium ${i ? "text-right" : ""}`}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {dists.map((d) => (
                    <tr key={d.district_code} className="border-b border-line last:border-0">
                      <th scope="row" className="py-1.5 font-medium">{d.district_code}</th>
                      <td className="tnum py-1.5 text-right">{fmtInt(d.residents)}</td>
                      <td className="tnum py-1.5 text-right">{d.due_residents == null ? "None" : `${fmtInt(d.due_residents)} (${fmtPct(d.due_share ?? null)})`}</td>
                      <td className="tnum py-1.5 text-right">{fmtInt(d.cases)}</td>
                      <td className="tnum py-1.5 text-right">{fmtInt(d.planned)}</td>
                      <td className="tnum py-1.5 text-right">{fmtInt(d.completed)}</td>
                      <td className="tnum py-1.5 text-right">{fmtPct(d.coverage)}</td>
                      <td className="tnum py-1.5 text-right">{fmtPct(d.closure_rate)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="mt-2 text-ink-muted">District figures are not available for your role.</p>
          )}
        </section>
      )}
    </div>
  );
}

function ReportsInner() {
  const { days: globalDays } = useRange();
  const params = useSearchParams();
  const [tab, setTab] = useState<Tab>("generate");
  // The form is the draft; `applied` is what the preview was generated from. Both start from a shared link when there is one.
  const [form, setForm] = useState<Def>(() => fromParams(params, globalDays));
  const [applied, setApplied] = useState<Def>(form);
  const [saved, setSaved] = useState<Stored[]>(() => (typeof window === "undefined" ? [] : readList(SAVED_KEY)));
  const [recent, setRecent] = useState<Stored[]>(() => (typeof window === "undefined" ? [] : readList(RECENT_KEY)));
  const [note, setNote] = useState("");

  const cc = useApi(`rp-cc-${applied.days}`, (signal) => api<CommandCenter>(`/analytics/command-center?days=${applied.days}`, { signal }));
  const pop = useApi("rp-pop", (signal) => api<PopulationOverview>("/analytics/population-overview", { signal }));
  const fun = useApi(`rp-funnel:${applied.district}`, (signal) => api<Funnel>("/analytics/funnel", { query: { district: applied.district || undefined }, signal }));
  const ex = useApi(`rp-exp:${applied.district}`, (signal) => api<PopulationExplorer>("/analytics/population-explorer", { query: { district: applied.district || undefined }, signal }));
  const geo = useApi(`rp-geo:${applied.district}`, (signal) => api<GeoResponse>("/analytics/geography", { query: applied.district ? { level: "district", code: applied.district } : { level: "country" }, signal }));
  const options = useProgrammes();
  const scope = useApi("meta-scope", (signal) => api<ScopeMeta>("/meta/scope", { signal }));

  const districtNames = new Map((scope.data?.data.districts ?? []).map((d) => [d.code, d.name]));
  const districtList = cc.data?.data.equity?.districts.map((d) => d.district_code) ?? [...districtNames.keys()];
  const programmes = (geo.data?.data.area.programmes as ProgrammeRow[] | undefined) ?? null;
  const busy = (cc.loading && !cc.data) || (pop.loading && !pop.data);
  const error = cc.error && pop.error ? cc.error : null;
  const ccData = cc.data?.data ?? null;
  const built: Built | null =
    ccData || pop.data
      ? { at: ccData?.generated_at ?? new Date().toISOString(), def: applied, cc: ccData, pop: pop.data?.data ?? null, funnel: fun.data?.data ?? null, ex: ex.data?.data ?? null, programmes }
      : null;

  const pick = (t: ReportType) => setForm((f) => ({ ...f, type: t, sections: TYPES[t].sections }));
  const remember = (d: Def) => {
    const next = [entry(d), ...readList(RECENT_KEY).filter((x) => !sameDef(x, d))].slice(0, 10);
    writeList(RECENT_KEY, next);
    setRecent(next);
  };
  const run = (d: Def) => {
    setForm(d);
    setApplied(d);
    setTab("generate");
    remember(d);
    setNote("");
  };
  const save = () => {
    const next = [entry(applied), ...readList(SAVED_KEY).filter((x) => !sameDef(x, applied))].slice(0, 20);
    writeList(SAVED_KEY, next);
    setSaved(next);
    setNote("Saved on this device. Open it from Saved reports.");
  };
  const unsave = (id: string) => {
    const next = readList(SAVED_KEY).filter((x) => x.id !== id);
    writeList(SAVED_KEY, next);
    setSaved(next);
  };
  const share = async (d: Def) => {
    const link = `${window.location.origin}/reports?${defQuery(d)}`;
    try {
      await navigator.clipboard.writeText(link);
      setNote("Link copied. It opens this report built from live figures, for the role of whoever opens it.");
    } catch {
      setNote(link);
    }
  };

  const end = built ? new Date(built.at) : new Date();
  const start = new Date(end.getTime() - applied.days * 86_400_000);
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  const meta = TYPES[applied.type];
  const progLabel = options.data?.data.programmes.find((p) => p.care_activity === applied.programme)?.label;
  const title = applied.type === "programme" && progLabel ? `${displayText(progLabel)} Programme Report` : meta.title;
  const label = (d: Def) => `${TYPES[d.type].label}${d.programme ? `, ${displayText(humanize(d.programme))}` : ""}${d.district ? `, ${districtNames.get(d.district) ?? d.district}` : ""}, ${RANGES.find((r) => r.days === d.days)?.label.toLowerCase() ?? `${d.days} days`}`;

  return (
    <>
      <PageHeader
        eyebrow="Reports"
        title="Reports and Insights"
        description="Create, explore and share reports on population health, programmes and impact."
        tabs={<PillTabs idBase="reports" label="Report views" items={TABS} value={tab} onChange={setTab} />}
      />

      {tab === "saved" || tab === "recent" ? (
        <div role="tabpanel" id="reports-panel" aria-labelledby={`reports-tab-${tab}`}>
          <Card as="section" aria-labelledby="rp-list">
            <CardHeader id="rp-list" icon={tab === "saved" ? "bookmark" : "clock"} title={tab === "saved" ? "Saved reports" : "Recent reports"} subtitle={tab === "saved" ? "Kept in this browser. A saved report stores its settings and is rebuilt from live figures each time." : "The reports you generated most recently in this browser."} />
            {(tab === "saved" ? saved : recent).length === 0 ? (
              <div className="px-5 pb-6">
                <EmptyState title={tab === "saved" ? "No saved reports yet" : "No reports generated yet"} icon="file">
                  {tab === "saved" ? "Generate a report and choose Save report to keep its settings here." : "Reports you generate appear here so you can run them again."}
                </EmptyState>
              </div>
            ) : (
              <ul className="divide-y divide-line px-5 pb-3">
                {(tab === "saved" ? saved : recent).map((s) => (
                  <li key={s.id} className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
                    <span className="grid size-10 shrink-0 place-items-center icon-tile">
                      <Icon name="file" className="size-5" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="font-semibold">{label(s)}</p>
                      <p className="text-xs text-ink-muted">{tab === "saved" ? "Saved" : "Generated"} {fmtDateTime(s.at)}</p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <button type="button" className={btnPrimary} onClick={() => run(s)}>Open</button>
                      <button type="button" className={btnSecondary} onClick={() => void share(s)}>
                        <Icon name="share" className="size-4" />
                        Copy link
                      </button>
                      {tab === "saved" && (
                        <button type="button" className={btnSecondary} onClick={() => unsave(s.id)} aria-label={`Remove ${label(s)}`}>
                          <Icon name="close" className="size-4" />
                          Remove
                        </button>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
            {note && <p role="status" className="px-5 pb-4 text-[0.8125rem] text-ink-muted">{note}</p>}
          </Card>
        </div>
      ) : (
        <div role="tabpanel" id="reports-panel" aria-labelledby="reports-tab-generate">
          <div className="mb-5 grid gap-4 xl:grid-cols-[minmax(0,26rem)_minmax(0,1fr)] [&>*]:min-w-0">
            <Card as="section" aria-labelledby="rp-create">
              <CardHeader id="rp-create" icon="file" title="Create a report" subtitle="Build a customised report for your population, programme or district." />
              <form
                className="space-y-4 px-5 pb-5"
                onSubmit={(e) => {
                  e.preventDefault();
                  run(form);
                }}
              >
                <FilterField label="Report type">
                  <select value={form.type} onChange={(e) => pick(e.target.value as ReportType)} className={selectClass}>
                    {(Object.keys(TYPES) as ReportType[]).map((k) => (
                      <option key={k} value={k}>{TYPES[k].label}</option>
                    ))}
                  </select>
                </FilterField>
                <FilterField label="Programme">
                    <select value={form.programme} onChange={(e) => setForm((f) => ({ ...f, programme: e.target.value }))} className={selectClass}>
                      <option value="">All programmes</option>
                      {(options.data?.data.programmes ?? []).map((p) => (
                        <option key={p.care_activity} value={p.care_activity}>{displayText(p.label)}</option>
                      ))}
                    </select>
                </FilterField>
                <div className="grid grid-cols-2 gap-3">
                  <FilterField label="Geography">
                    <select value={form.district} onChange={(e) => setForm((f) => ({ ...f, district: e.target.value }))} className={selectClass}>
                      <option value="">All districts</option>
                      {districtList.map((d) => (
                        <option key={d} value={d}>{districtNames.get(d) ? `${districtNames.get(d)} (${d})` : d}</option>
                      ))}
                    </select>
                  </FilterField>
                  <FilterField label="Time period">
                    <select value={form.days} onChange={(e) => setForm((f) => ({ ...f, days: Number(e.target.value) }))} className={selectClass}>
                      {RANGES.map((r) => (
                        <option key={r.days} value={r.days}>{r.label}</option>
                      ))}
                    </select>
                  </FilterField>
                </div>
                <fieldset>
                  <legend className="mb-1.5 text-xs font-semibold text-ink-soft">Report focus</legend>
                  <div className="grid grid-cols-2 gap-2">
                    {SECTIONS.map((s) => {
                      const on = form.sections.includes(s.id);
                      return (
                        <label key={s.id} className={`tile flex min-h-11 cursor-pointer items-center gap-2 px-3 text-[0.8125rem] ${on ? "bg-brand-soft" : ""}`}>
                          <input type="checkbox" checked={on} onChange={() => setForm((f) => ({ ...f, sections: on ? f.sections.filter((x) => x !== s.id) : [...f.sections, s.id] }))} className="size-4 accent-brand" />
                          {s.label}
                        </label>
                      );
                    })}
                  </div>
                </fieldset>
                {error && <ErrorPanel error={error instanceof ApiError ? error : new ApiError("INTERNAL_ERROR", "The report could not be built.", 0)} title="Could not build the report" />}
                <button type="submit" className={`${btnPrimary} w-full`} disabled={busy}>
                  <Icon name="sparkles" className="size-4" />
                  {busy ? "Generating…" : "Generate report"}
                </button>
              </form>
            </Card>

            <Card as="section" aria-labelledby="rp-preview">
              <CardHeader id="rp-preview" title="Report preview" />
              {built && (
                <div className="flex flex-wrap gap-2 px-5 pb-4">
                  <button type="button" className={btnSecondary} onClick={save}>
                    <Icon name="bookmark" className="size-4" />
                    Save report
                  </button>
                  <button type="button" className={btnSecondary} onClick={() => void share(applied)}>
                    <Icon name="share" className="size-4" />
                    Copy link
                  </button>
                  <button type="button" className={btnSecondary} onClick={() => download(built)}>
                    <Icon name="file-down" className="size-4" />
                    Download CSV
                  </button>
                  <button type="button" className={btnSecondary} onClick={() => window.print()}>
                    <Icon name="print" className="size-4" />
                    Print
                  </button>
                </div>
              )}
              {note && <p role="status" className="px-5 pb-3 text-[0.8125rem] break-all text-ink-muted">{note}</p>}
              <div className="grid gap-5 px-5 pb-5 md:grid-cols-[minmax(0,1fr)_minmax(0,16rem)]">
                <div className="grad-brand-soft flex min-h-64 flex-col justify-between rounded-card border border-line p-6">
                  <div>
                    <p className="eyebrow">Anticipa</p>
                    <h3 className="mt-3 font-display text-[1.9rem] leading-[1.15] font-bold">{title}</h3>
                    <p className="mt-2 text-ink-soft">{meta.subtitle}</p>
                  </div>
                  <p className="mt-6 flex flex-wrap gap-2">
                    <Pill tone="brand">{fmtDate(iso(start))} to {fmtDate(iso(end))}</Pill>
                    {applied.district && <Pill tone="brand">{districtNames.get(applied.district) ?? applied.district}</Pill>}
                  </p>
                </div>
                <div>
                  <h3 className="font-display text-lg font-bold">Report sections</h3>
                  <ul className="mt-2 divide-y divide-line">
                    {[{ id: "summary", label: "Executive summary", icon: "file" as IconName }, ...SECTIONS.filter((s) => applied.sections.includes(s.id))].map((s) => (
                      <li key={s.id} className="flex items-center gap-3 py-2.5">
                        <Icon name={s.icon} className="size-[1.1rem] text-brand-ink" />
                        {s.label}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
              {built ? <ReportBody b={built} /> : <div className="skeleton mx-5 mb-5 h-40" role="status" aria-label="Building the report" />}
            </Card>
          </div>

          <Card as="section" aria-labelledby="rp-quick" className="mb-5">
            <div className="flex flex-wrap items-center gap-x-6 gap-y-3 px-5 py-4">
              <div className="flex items-center gap-3">
                <span className="grid size-10 place-items-center icon-tile">
                  <Icon name="zap" className="size-5" />
                </span>
                <div>
                  <h2 id="rp-quick" className="font-display text-lg leading-6 font-bold">Quick reports</h2>
                  <p className="text-xs text-ink-muted">One click reports for common needs.</p>
                </div>
              </div>
              <div className="flex flex-1 flex-wrap gap-2.5">
                {QUICK.map((q) => (
                  <button
                    key={q}
                    type="button"
                    disabled={busy}
                    onClick={() => run({ type: q, district: form.district, programme: q === "programme" ? form.programme : "", days: form.days, sections: TYPES[q].sections })}
                    className="glass-strong inline-flex min-h-11 items-center gap-2 rounded-full! px-5 text-sm font-semibold text-ink hover:text-brand-ink disabled:opacity-50"
                  >
                    <Icon name="chart" className="size-4 text-brand-ink" />
                    {TYPES[q].label}
                  </button>
                ))}
              </div>
            </div>
          </Card>
        </div>
      )}
    </>
  );
}

export default function ReportsPage() {
  return (
    <Suspense fallback={null}>
      <ReportsInner />
    </Suspense>
  );
}
