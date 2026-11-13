"use client";

// Programs and impact: pick a preventive care programme (a guideline care activity) and optionally a district, see how many
// eligible cases move through planning, outreach, attendance and closure, the district ranking and cohort picture, and a
// plain arithmetic projection. Every stage and district opens the real cases behind it. Real data only: GET /guidelines,
// GET /care-gaps counts per stage, /analytics/command-center (districts, priority), /analytics/capacity (seats). A tab holds
// the measured quality dashboard. Route: /evaluation. Not shown because no endpoint backs them: impact score, median time
// to attend, period on period change and a district map (see data/reports/ui-data-gaps.json).
// Purpose: show judges what the programme achieved and where it lags.

import { useState } from "react";
import { api, type ApiError } from "@/lib/api";
import { fmtInt, humanize } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { Card, CardHeader, DataLabel, ErrorPanel, KpiTile, PageHeader, Pill, btnSecondary, selectClass, DuoIcon, Icon } from "@/components/ui";
import type { DuoName } from "@/components/ui";
import { EvaluationDashboard } from "./evaluation";
import { PopulationFunnel } from "./funnel";
import { ProvenImpact } from "./proven-impact";
import { CaseDrawer, GrowBar, InfoTip, STAGE_STATUS, StageLink, countCases, type CaseQuery } from "./interactive";
import { PillTabs, TabPanel, useCapacity, useCommandCenter, useProgrammes, type PillTab } from "./kit";
import type { CommandCenter, GeoArea, Geography } from "./payloads";
import { fmtShare } from "./shared";

type Tab = "impact" | "proven" | "quality";
const TABS: PillTab<Tab>[] = [
  { key: "impact", label: "Impact", icon: "gauge" },
  { key: "proven", label: "Impact and economics", icon: "trend-up" },
  { key: "quality", label: "Measured quality", icon: "flask" },
];

const STAGES: { key: keyof typeof STAGE_STATUS; label: string; icon: DuoName }[] = [
  { key: "eligible", label: "Eligible", icon: "eligible" },
  { key: "planned", label: "Planned", icon: "calendar" },
  { key: "contacted", label: "Contacted", icon: "engage" },
  { key: "attended", label: "Attended", icon: "attend" },
  { key: "completed", label: "Completed", icon: "complete" },
];

const share = (a: number, b: number) => (b > 0 ? a / b : null);

function Gauge({ value, label }: { value: number | null; label: string }) {
  const L = Math.PI * 50;
  const v = Math.max(0, Math.min(1, value ?? 0));
  return (
    <div className="text-center">
      <svg role="img" aria-label={`${label}: ${value == null ? "not available" : fmtShare(value)}`} viewBox="0 0 120 74" className="mx-auto w-44">
        <path d="M10 62A50 50 0 0 1 110 62" fill="none" stroke="#e9e5f5" strokeWidth="11" strokeLinecap="round" />
        {value != null && <path d="M10 62A50 50 0 0 1 110 62" fill="none" stroke="#7b5bd6" strokeWidth="11" strokeLinecap="round" strokeDasharray={`${L * v} ${L}`} style={{ transition: "stroke-dasharray 700ms ease" }} />}
        <text x="60" y="52" textAnchor="middle" fontSize="22" fontWeight="700" fill="#0f1b2d">
          {value == null ? "N/A" : fmtShare(value)}
        </text>
      </svg>
      <p className="-mt-1 font-medium text-ink-soft">{label}</p>
    </div>
  );
}

function ImpactView() {
  const [prog, setProg] = useState<string>("");
  const [district, setDistrict] = useState<string>("");
  const [target, setTarget] = useState<number | null>(null);
  const [drawer, setDrawer] = useState<{ title: string; query: CaseQuery } | null>(null);
  const programmes = useProgrammes();
  const cc = useCommandCenter();
  const cap = useCapacity();
  const base: CaseQuery = { care_activity: prog || undefined, district: district || undefined };
  const counts = useApi(`impact:${prog}:${district}`, async (s) => {
    const entries = await Promise.all(Object.entries(STAGE_STATUS).map(async ([k, st]) => [k, await countCases({ ...base, status: st }, s)] as const));
    return Object.fromEntries(entries) as Record<string, number>;
  });

  const rows = programmes.data?.data.programmes ?? [];
  const c = counts.data;
  const eligible = c?.eligible ?? null;
  const coverage = c ? share(c.planned, c.eligible) : null;
  const closure = c ? share(c.completed, c.eligible) : null;
  const equity = cc.data?.data.equity ?? null;
  const gapPp = equity?.coverage.gap != null ? Math.round(equity.coverage.gap * 1000) / 10 : null;
  const slider = target ?? Math.min(100, Math.round(((coverage ?? 0) * 100 + 10) / 5) * 5);
  const extra = c ? Math.max(0, Math.ceil((slider / 100) * c.eligible) - c.planned) : null;
  const seats = cap.data?.data.capacity.remaining_seats ?? null;
  const scope = [prog && humanize(prog), district && `district ${district}`].filter(Boolean).join(", ");
  const openStage = (k: string, label: string) => setDrawer({ title: `${label} cases${scope ? `, ${scope}` : ""}`, query: { ...base, status: STAGE_STATUS[k] } });

  return (
    <div className="space-y-5">
      <div role="group" aria-label="Programme" className="flex flex-wrap gap-2">
        {[{ key: "", label: "All programmes", n: null as number | null }, ...rows.map((r) => ({ key: r.care_activity, label: humanize(r.care_activity), n: r.cases_total as number | null }))].map((p) => (
          <button
            key={p.key || "all"}
            type="button"
            aria-pressed={prog === p.key}
            onClick={() => setProg(p.key)}
            className={`inline-flex min-h-11 items-center gap-2 rounded-full border px-5 font-semibold transition-colors ${prog === p.key ? "border-brand bg-brand-soft text-brand-ink" : "border-line bg-surface text-ink-soft hover:bg-canvas"}`}
          >
            <DuoIcon name={p.key ? "ribbon" : "layers"} className="size-5" />
            {p.label}
            {p.n != null && <span className="tnum text-xs font-normal text-ink-muted">{fmtInt(p.n)}</span>}
          </button>
        ))}
        {district && (
          <button type="button" onClick={() => setDistrict("")} className={`${btnSecondary} rounded-full`} aria-label={`District ${district} is selected. Clear it`}>
            District {district}
            <Icon name="close" className="size-4" />
          </button>
        )}
      </div>

      {counts.error ? (
        <ErrorPanel error={counts.error} onRetry={counts.reload} title="Could not load programme figures" />
      ) : (
        <>
          <section aria-label="Programme figures" className="glass-strong card grid gap-4 p-4 sm:grid-cols-2 xl:grid-cols-4">
            <KpiTile icon="users" label="Eligible residents" value={eligible == null ? null : fmtInt(eligible)} basis={`Care gap cases opened${scope ? ` for ${scope}` : ""}.`} loading={!c} badge={<DataLabel kind="live" />} />
            <KpiTile icon="calendar-check" label="Attended" value={c ? fmtInt(c.attended) : null} basis={c ? `${fmtShare(share(c.attended, c.eligible))} of eligible. Attended or completed.` : undefined} loading={!c} />
            <KpiTile icon="target" label="Care gaps closed" value={c ? fmtInt(c.completed) : null} basis={c ? `${fmtShare(closure)} of eligible.` : undefined} loading={!c} />
            <KpiTile icon="gauge" label="Planned coverage" value={coverage == null ? (c ? "Not available" : null) : fmtShare(coverage)} basis={c ? `${fmtInt(c.planned)} of ${fmtInt(c.eligible)} cases reached planning.` : undefined} loading={!c} />
          </section>

          <div className="grid gap-5 xl:grid-cols-[1.7fr_1fr] *:min-w-0">
            <Card as="section" aria-labelledby="cj" className="glass-strong">
              <CardHeader id="cj" icon="layers" title="Care journey" subtitle="Cases currently at or beyond each stage. Select a stage to see its cases." aside={<DataLabel kind="live" />} />
              <ol aria-label="Care journey stages" className="flex flex-wrap items-start justify-between gap-3 px-5 pb-6">
                {STAGES.map((s, i) => (
                  <li key={s.key} data-stage={s.key} className="flex min-w-24 flex-1 items-center gap-1">
                    <button type="button" onClick={() => openStage(s.key, s.label)} className="flex-1 rounded-card p-2 text-center transition-colors hover:bg-brand-soft">
                      <span className="mx-auto grid w-fit place-items-center">
                        <span aria-hidden="true" className="grid size-16 place-items-center rounded-3xl border border-white bg-linear-to-b from-white to-brand-soft text-brand-ink shadow-card ring-1 ring-black/5">
                          <DuoIcon name={s.icon} className="size-9" />
                        </span>
                      </span>
                      <span className="mt-2 block font-medium">{s.label}</span>
                      <span className="tnum block font-serif text-xl font-semibold">{c ? fmtInt(c[s.key]) : "…"}</span>
                      <span className="tnum block text-xs text-ink-muted">{c && i > 0 ? `(${fmtShare(share(c[s.key], c.eligible))})` : ""}</span>
                      <span className="sr-only">. Open the cases.</span>
                    </button>
                    {i < STAGES.length - 1 && <StageLink />}
                  </li>
                ))}
              </ol>
            </Card>

            <Card as="section" aria-labelledby="ig" className="glass-strong">
              <CardHeader id="ig" icon="sparkles" title="Impact at a glance" aside={<DataLabel kind="live" />} />
              <div className="grid items-center gap-4 px-5 pb-5 sm:grid-cols-[auto_1fr]">
                <Gauge value={closure} label="Closure rate" />
                <dl className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <dt className="text-ink-soft">Coverage</dt>
                    <dd className="tnum font-semibold">{fmtShare(coverage)}</dd>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <dt className="text-ink-soft">Care gaps closed</dt>
                    <dd className="tnum font-semibold">{c ? fmtInt(c.completed) : "Not available"}</dd>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <dt className="text-ink-soft">Median time to attend</dt>
                    <dd className="font-semibold text-ink-muted">Not available</dd>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <dt className="text-ink-soft">
                      Equity gap between districts <InfoTip text="Highest minus lowest district planned coverage, in percentage points. Districts are assigned codes, not real geography." />
                    </dt>
                    <dd className="tnum font-semibold">{gapPp == null ? "Not available" : `${gapPp} points`}</dd>
                  </div>
                </dl>
              </div>
            </Card>
          </div>

          <div className="grid gap-5 xl:grid-cols-3 *:min-w-0">
            <GeoRanking prog={prog} selected={district} onSelect={(d) => setDistrict(d === district ? "" : d)} legacy={<Districts cc={cc.data?.data} error={cc.error} reload={cc.reload} selected={district} onSelect={(d) => setDistrict(d === district ? "" : d)} />} />
            <Equity cc={cc.data?.data} onOpen={(title, q) => setDrawer({ title, query: { ...base, ...q } })} />
            <Card as="section" aria-labelledby="ip" className="glass-strong">
              <CardHeader id="ip" icon="zap" title="Impact projection" subtitle="What if we plan for more of the eligible cases?" aside={<Pill tone="warn">Arithmetic, not a forecast</Pill>} />
              <div className="space-y-4 px-5 pb-5">
                <div>
                  <label htmlFor="target" className="flex items-center justify-between font-medium">
                    <span>Target planned coverage</span>
                    <output htmlFor="target" className="tnum rounded-full border border-line bg-surface px-3 py-0.5 font-semibold">{slider}%</output>
                  </label>
                  <input id="target" type="range" min={0} max={100} step={5} value={slider} onChange={(e) => setTarget(Number(e.target.value))} className="mt-2 w-full accent-brand" />
                </div>
                <dl className="grid grid-cols-2 gap-3 rounded-control border border-line bg-canvas p-3">
                  <div>
                    <dt className="text-xs text-ink-muted">More cases to plan</dt>
                    <dd className="tnum text-lg font-bold">{extra == null ? "Not available" : `+${fmtInt(extra)}`}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-ink-muted">Seats still open</dt>
                    <dd className="tnum text-lg font-bold">{seats == null ? "Not available" : fmtInt(seats)}</dd>
                  </div>
                </dl>
                <p className="text-xs text-ink-muted">
                  {extra != null && seats != null ? (extra > seats ? `Short by ${fmtInt(extra - seats)} seats at the current capacity.` : "The open seats would cover the extra cases.") : "Needs both case counts and capacity."} Cases to plan = target share of eligible cases minus cases already planned.
                </p>
              </div>
            </Card>
          </div>
        </>
      )}
      <PopulationFunnel />
      {programmes.error && <ErrorPanel error={programmes.error} onRetry={programmes.reload} title="Could not load the programme list" />}
      <CaseDrawer open={drawer != null} onClose={() => setDrawer(null)} title={drawer?.title ?? ""} query={drawer?.query ?? {}} />
    </div>
  );
}

type Measure = "coverage" | "closure_rate";
/** The measure of an area, for one programme when one is picked (the snapshot carries every programme per area). */
const areaValue = (a: GeoArea, prog: string, m: Measure): number | null => (prog ? (a.programmes.find((p) => p.care_activity === prog)?.[m] ?? null) : a[m]);
const areaCases = (a: GeoArea, prog: string): number => (prog ? (a.programmes.find((p) => p.care_activity === prog)?.cases_total ?? 0) : a.cases_total);

function Row({ rank, name, value, cases, max, on, onClick }: { rank?: number; name: string; value: number | null; cases: number; max: number; on: boolean; onClick: () => void; label?: string }) {
  return (
    <button type="button" aria-pressed={on} onClick={onClick} className={`flex w-full items-center gap-3 rounded-control px-2.5 py-2 text-left transition-colors ${on ? "bg-brand-soft ring-1 ring-brand/40" : "hover:bg-canvas"}`}>
      {rank != null && <span className="tnum w-5 text-xs text-ink-muted">{rank}</span>}
      <span className="min-w-0 flex-1 truncate font-semibold">{name}</span>
      <span className="h-2.5 w-20 shrink-0 overflow-hidden rounded-full bg-brand-soft sm:w-28" aria-hidden="true">
        <GrowBar pct={((value ?? 0) / max) * 100} />
      </span>
      <span className="tnum w-12 shrink-0 text-right font-semibold">{fmtShare(value)}</span>
      <span className="tnum hidden w-20 shrink-0 text-right text-xs text-ink-muted sm:block">{fmtInt(cases)} cases</span>
    </button>
  );
}

function StateDistricts({ code, prog, measure, selected, onSelect }: { code: string; prog: string; measure: Measure; selected: string; onSelect: (d: string) => void }) {
  const res = useApi(`geo:state:${code}`, (s) => api<Geography>("/analytics/geography", { query: { level: "state", code }, signal: s }));
  const rows = [...(res.data?.data.areas ?? [])].sort((a, b) => (areaValue(b, prog, measure) ?? -1) - (areaValue(a, prog, measure) ?? -1));
  const max = Math.max(0.0001, ...rows.map((r) => areaValue(r, prog, measure) ?? 0));
  if (res.error) return <p className="px-3 py-2 text-ink-muted">Could not load the districts of this state.</p>;
  if (!res.data) return <div className="skeleton mx-3 my-2 h-12" />;
  return (
    <ol aria-label="Districts" className="mb-2 ml-6 space-y-1 border-l border-line pl-2">
      {rows.map((r, i) => (
        <li key={r.code}>
          <Row rank={i + 1} name={`${r.name} (${r.code})`} value={areaValue(r, prog, measure)} cases={areaCases(r, prog)} max={max} on={selected === r.code} onClick={() => onSelect(r.code)} label={`${r.code}, ${r.name}`} />
        </li>
      ))}
    </ol>
  );
}

/** Pilot states ranked from the precomputed snapshot; a state opens its districts; a district filters the figures above. */
function GeoRanking({ prog, selected, onSelect, legacy }: { prog: string; selected: string; onSelect: (d: string) => void; legacy: React.ReactNode }) {
  const [measure, setMeasure] = useState<Measure>("coverage");
  const [open, setOpen] = useState<string | null>(null);
  const res = useApi("geo:country", (s) => api<Geography>("/analytics/geography", { query: { level: "country" }, signal: s }));
  if (res.error) return <>{legacy}</>;
  const states = (res.data?.data.areas ?? []).filter((a) => a.coverage_status === "pilot").sort((a, b) => (areaValue(b, prog, measure) ?? -1) - (areaValue(a, prog, measure) ?? -1));
  const max = Math.max(0.0001, ...states.map((a) => areaValue(a, prog, measure) ?? 0));
  return (
    <Card as="section" aria-labelledby="cd" className="glass-strong">
      <CardHeader
        id="cd"
        icon="pin"
        title="Districts ranked"
        subtitle="Open a state, then select a district to filter every figure above."
        aside={
          <select aria-label="District measure" value={measure} onChange={(e) => setMeasure(e.target.value as Measure)} className={`${selectClass} min-h-9 py-0`}>
            <option value="coverage">Planned coverage</option>
            <option value="closure_rate">Closure rate</option>
          </select>
        }
      />
      <div className="px-5 pb-5">
        {!res.data ? (
          <div className="skeleton h-40" role="status" aria-label="Loading states" />
        ) : states.length === 0 ? (
          <p className="text-ink-muted">No state has residents yet.</p>
        ) : (
          <ol aria-label="States" className="space-y-1">
            {states.map((a, i) => (
              <li key={a.code}>
                <Row rank={i + 1} name={a.name} value={areaValue(a, prog, measure)} cases={areaCases(a, prog)} max={max} on={open === a.code} onClick={() => setOpen(open === a.code ? null : a.code)} label={`${a.name}, ${areaCases(a, prog)} cases`} />
                {open === a.code && <StateDistricts code={a.code} prog={prog} measure={measure} selected={selected} onSelect={onSelect} />}
              </li>
            ))}
          </ol>
        )}
        <p className="mt-3 text-xs text-ink-muted">Residents are placed in pilot districts as part of the data design. These figures describe those residents, not real district health.</p>
      </div>
    </Card>
  );
}

function Districts({ cc, error, reload, selected, onSelect }: { cc: CommandCenter | undefined; error: ApiError | null; reload: () => void; selected: string; onSelect: (d: string) => void }) {
  const [metric, setMetric] = useState<"coverage" | "closure_rate">("coverage");
  const rows = [...(cc?.equity?.districts ?? [])].sort((a, b) => (b[metric] ?? -1) - (a[metric] ?? -1));
  const max = Math.max(0.0001, ...rows.map((r) => r[metric] ?? 0));
  return (
    <Card as="section" aria-labelledby="cd" className="glass-strong">
      <CardHeader
        id="cd"
        icon="pin"
        title="Districts ranked"
        subtitle="Select a district to filter every figure above."
        aside={
          <select aria-label="District measure" value={metric} onChange={(e) => setMetric(e.target.value as typeof metric)} className={`${selectClass} min-h-9 py-0`}>
            <option value="coverage">Planned coverage</option>
            <option value="closure_rate">Closure rate</option>
          </select>
        }
      />
      <div className="px-5 pb-5">
        {error ? (
          <ErrorPanel error={error} onRetry={reload} title="Could not load districts" />
        ) : !cc ? (
          <div className="skeleton h-40" />
        ) : !cc.equity ? (
          <p className="text-ink-muted">Your role cannot view district figures.</p>
        ) : rows.length === 0 ? (
          <p className="text-ink-muted">No district has cases yet.</p>
        ) : (
          <ol aria-label="Districts" className="space-y-1.5">
            {rows.map((r, i) => {
              const v = r[metric];
              const on = selected === r.district_code;
              return (
                <li key={r.district_code}>
                  <button type="button" aria-pressed={on} onClick={() => onSelect(r.district_code)} className={`flex w-full items-center gap-3 rounded-control px-2.5 py-2 text-left transition-colors ${on ? "bg-brand-soft ring-1 ring-brand/40" : "hover:bg-canvas"}`}>
                    <span className="tnum w-5 text-xs text-ink-muted">{i + 1}</span>
                    <span className="w-14 shrink-0 font-semibold">{r.district_code}</span>
                    <span className="h-2.5 min-w-8 flex-1 overflow-hidden rounded-full bg-brand-soft" aria-hidden="true">
                      <GrowBar pct={((v ?? 0) / max) * 100} />
                    </span>
                    <span className="tnum w-12 shrink-0 text-right font-semibold">{fmtShare(v)}</span>
                    <span className="tnum hidden w-20 shrink-0 text-right text-xs text-ink-muted sm:block">{fmtInt(r.cases)} cases</span>
                  </button>
                </li>
              );
            })}
          </ol>
        )}
        <p className="mt-3 text-xs text-ink-muted">District codes are assigned values. A map is not drawn because no boundaries are stored.</p>
      </div>
    </Card>
  );
}

function Equity({ cc, onOpen }: { cc: CommandCenter | undefined; onOpen: (title: string, q: CaseQuery) => void }) {
  const rows = (cc?.gaps_by_priority.items ?? []).map((p) => ({ key: p.priority, label: `${p.priority === "elevated" ? "Medium" : humanize(p.priority)} priority`, value: share(p.completed, p.total), q: { priority: p.priority } as CaseQuery }));
  const lo = cc?.equity?.closure_rate.min;
  const hi = cc?.equity?.closure_rate.max;
  const all = [...rows, ...(hi ? [{ key: "hi", label: `Highest district, ${hi.district_code}`, value: hi.value, q: { district: hi.district_code } as CaseQuery }] : []), ...(lo ? [{ key: "lo", label: `Lowest district, ${lo.district_code}`, value: lo.value, q: { district: lo.district_code } as CaseQuery }] : [])];
  const top = Math.max(0.0001, ...all.map((r) => r.value ?? 0));
  return (
    <Card as="section" aria-labelledby="eq" className="glass-strong">
      <CardHeader id="eq" icon="scale" title="Equity and cohort performance" subtitle="Closure rate: cases completed of cases opened." aside={<DataLabel kind="live" />} />
      <div className="px-5 pb-5">
        {!cc ? (
          <div className="skeleton h-40" />
        ) : (
          <ul aria-label="Closure rate by cohort" className="space-y-1.5">
            {all.map((r) => (
              <li key={r.key}>
                <button type="button" onClick={() => onOpen(r.label, r.q)} className="flex w-full items-center gap-3 rounded-control px-2 py-2 text-left hover:bg-canvas">
                  <span className="w-36 shrink-0 text-ink-soft">{r.label}</span>
                  <span className="h-2.5 min-w-8 flex-1 overflow-hidden rounded-full bg-brand-soft" aria-hidden="true">
                    <GrowBar pct={Math.max(1, ((r.value ?? 0) / top) * 100)} />
                  </span>
                  <span className="tnum w-14 shrink-0 text-right font-semibold">{fmtShare(r.value)}</span>
                  <span className="sr-only">. Open the cases.</span>
                </button>
              </li>
            ))}
            {all.length === 0 && <li className="text-ink-muted">No cohort has cases yet.</li>}
          </ul>
        )}
      </div>
    </Card>
  );
}

export function ProgramsImpact() {
  const [tab, setTab] = useState<Tab>("impact");
  return (
    <>
      <PageHeader title="Programs and impact" description="Track, compare and improve preventive care programs across the population." actions={<PillTabs tabs={TABS} active={tab} onChange={setTab} label="Impact views" panelId="impact-panel" />} />
      <TabPanel id="impact-panel" label={TABS.find((t) => t.key === tab)!.label}>
        {tab === "impact" ? <ImpactView /> : tab === "proven" ? <ProvenImpact /> : <EvaluationDashboard embedded />}
      </TabPanel>
    </>
  );
}
