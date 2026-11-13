"use client";

import Link from "next/link";
import { useEffect } from "react";
import { api, isMissingEndpoint } from "@/lib/api";
import { fmtDateTime, fmtDayMonth, fmtInt, fmtPct, humanize } from "@/lib/format";
import type { CcAlertRule, CcDistrict, CcMetric, CommandCenter } from "@/lib/types";
import type { Funnel } from "@/components/features/payloads";
import { useApi } from "@/lib/use-api";
import { useCan, useRange, useScope } from "@/components/app-shell";
import { OperationsSnapshot, ProgramsGlance } from "@/components/command-widgets";
import { IndiaMap } from "@/components/india-map";
import { useGeo } from "@/lib/geography";
import type { GeoArea } from "@/lib/types";
import { useState } from "react";
import { displayText } from "@/lib/display";
import {
  AreaTrend,
  Card,
  CardHeader,
  Columns,
  DataLabel,
  ErrorPanel,
  FunnelStack,
  Icon,
  KpiTile,
  PageHeader,
  Pill,
  Unavailable,
  btnPrimary,
  btnSecondary,
  type IconName,
} from "@/components/ui";

const kpi = (cc: CommandCenter | undefined, key: string): CcMetric | undefined => cc?.kpis.find((k) => k.key === key);
const PRIORITY_LABEL: Record<string, string> = { high: "High", elevated: "Medium", routine: "Low" };
const PRIORITY_ORDER = ["high", "elevated", "routine"];
const per100 = (d: CcDistrict) => (d.residents > 0 ? (d.cases / d.residents) * 100 : null);

function AlertRow({ r }: { r: CcAlertRule }) {
  const critical = r.severity === "critical";
  const icon: IconName = r.status === "alert" ? "warning" : r.status === "ok" ? "check-circle" : "info";
  const tone = r.status === "alert" ? (critical ? "text-danger" : "text-warn") : r.status === "ok" ? "text-ok" : "text-ink-muted";
  return (
    <li className="flex items-start gap-3 py-3">
      <Icon name={icon} className={`mt-0.5 size-5 ${tone}`} />
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{displayText(r.title)}</p>
        <p className="text-[0.8125rem] text-ink-muted">{displayText(r.message)}</p>
      </div>
      {r.status === "alert" ? (
        <Pill tone={critical ? "danger" : "warn"}>{critical ? "High" : "Medium"}</Pill>
      ) : r.status === "ok" ? (
        <Pill tone="ok">OK</Pill>
      ) : (
        <Pill tone="neutral">No data</Pill>
      )}
    </li>
  );
}

/** Reads the care gap funnel; a role without access simply does not get the card. */
function CareJourneyCard({ query, label }: { query: string; label?: string }) {
  const res = useApi(`cc-funnel${query}`, (signal) => api<Funnel>(`/analytics/funnel${query ? `?${query}` : ""}`, { signal }));
  const f = res.data?.data;
  const first = f?.stages[0]?.count ?? 0;
  return (
    <Card as="section" aria-labelledby="cc-journey">
      <CardHeader id="cc-journey" title="Care journey" subtitle="From resident to closed care gap" aside={label ? <Pill tone="brand" icon="pin">{label}</Pill> : undefined} />
      <div className="px-5 pb-5">
        {res.error ? (
          <p className="py-8 text-center text-ink-muted">The care journey is not available for your role.</p>
        ) : !f ? (
          <div className="skeleton h-64 w-full" role="status" aria-label="Loading care journey" />
        ) : (
          <FunnelStack
            ariaLabel="Care journey stages"
            tableCaption="Residents at each stage of the care journey"
            rows={f.stages.map((s, i) => ({ label: s.label, value: s.count, sub: i && first ? `${((s.count / first) * 100).toFixed(1)}%` : undefined }))}
          />
        )}
      </div>
    </Card>
  );
}

function Figures({ area }: { area: GeoArea | null | undefined }) {
  if (!area) return <div className="skeleton h-20 w-full" role="status" aria-label="Loading figures" />;
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4 xl:grid-cols-2 [&>*]:min-w-0">
      {[
        ["Population", fmtInt(area.population)],
        ["Due for care", fmtInt(area.due_residents)],
        ["Open care gaps", fmtInt(area.open_care_gaps)],
        ["Closure rate", area.closure_rate == null ? "None" : fmtPct(area.closure_rate)],
      ].map(([k, v]) => (
        <div key={k}>
          <dt className="text-xs text-ink-muted">{k}</dt>
          <dd className="tnum font-semibold">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function ProgrammeBars({ area }: { area: GeoArea }) {
  const rows = area.programmes.filter((p) => p.cases_total > 0).slice(0, 4);
  if (!rows.length) return null;
  return (
    <ul aria-label="Closure rate by program" className="mt-3 space-y-2">
      {rows.map((p) => (
        <li key={p.care_activity} className="flex items-center gap-2 text-[0.8125rem]">
          <span className="w-24 shrink-0 truncate text-ink-soft" title={p.label}>
            {p.label}
          </span>
          <span className="h-2 min-w-6 flex-1 overflow-hidden rounded-full bg-neutral-bg" aria-hidden="true">
            <span className="block h-full rounded-full" style={{ width: `${(p.closure_rate ?? 0) * 100}%`, background: "linear-gradient(90deg, var(--chart-violet), var(--chart-mid))" }} />
          </span>
          <span className="tnum w-10 shrink-0 text-right font-semibold">{p.closure_rate == null ? "None" : fmtPct(p.closure_rate)}</span>
        </li>
      ))}
    </ul>
  );
}

/** Change versus the period before (command-center ?compare=previous). Shows "No earlier period yet" when there is no earlier figure. */
function Change({ cc, kpiKey, pp = false, note = false }: { cc: CommandCenter | undefined; kpiKey: string; pp?: boolean; note?: boolean }) {
  const c = cc?.comparison;
  const k = c?.kpis?.[kpiKey];
  if (!c?.available || !k || k.previous === null || k.previous === 0 || k.delta === null) return note ? <span className="text-xs text-ink-muted">No earlier period yet</span> : null;
  const v = pp ? (k.delta_pp ?? 0) : k.delta;
  const size = pp ? `${Math.abs(v).toFixed(1)} pp` : fmtInt(Math.abs(v));
  return (
    <Pill tone="neutral" icon={v < 0 ? "trend-down" : "trend-up"}>
      {v === 0 ? "No change" : `${v < 0 ? "Down" : "Up"} ${size}`} vs {c.window_days} days ago
    </Pill>
  );
}

export default function CommandCenterPage() {
  const can = useCan();
  const { days } = useRange();
  const geo = useGeo();
  const { scope, setScope, setDistricts } = useScope();
  const gd = geo.data?.districts ?? [];
  const gStates = geo.data?.states ?? [];
  const chosenArea = gd.find((d) => d.code === scope) ?? null;
  const [pickedState, setPickedState] = useState<string | null>(null);
  const stateName = chosenArea ? (chosenArea.state ?? null) : pickedState;
  const stateArea = gStates.find((x) => x.name === stateName) ?? null;
  const scopeArea = chosenArea ?? stateArea;
  const scopeLabel = chosenArea ? chosenArea.name : (stateName ?? "");
  // Every Command Center widget filters on the selection: the backend takes district= or state= on command-center, funnel and programmes.
  const scopeQuery = chosenArea ? `district=${chosenArea.code}` : stateArea ? `state=${encodeURIComponent(stateArea.code)}` : "";
  const res = useApi(`command-center-${days}-cmp-${scopeQuery}`, (signal) => api<CommandCenter>(`/analytics/command-center?days=${days}&compare=previous${scopeQuery ? `&${scopeQuery}` : ""}`, { signal }));
  const cc = res.data?.data;
  const loading = res.loading && !cc;
  const codesKey = gd.map((d) => d.code).join(",");
  useEffect(() => {
    setDistricts(codesKey ? codesKey.split(",") : []);
  }, [codesKey, setDistricts]);

  const total = kpi(cc, "residents_total");
  const withGaps = kpi(cc, "residents_with_open_gaps");
  const openGaps = kpi(cc, "care_gaps_open");
  const allGaps = kpi(cc, "care_gaps_total");
  const closed = kpi(cc, "gaps_closed");
  const closure = kpi(cc, "closure_rate");
  const outreach = kpi(cc, "in_outreach");
  const share = (n: number | null | undefined, d: number | null | undefined) => (n != null && d ? fmtPct(n / d) : null);

  const priority = [...(cc?.gaps_by_priority.items ?? [])].sort((a, b) => PRIORITY_ORDER.indexOf(a.priority) - PRIORITY_ORDER.indexOf(b.priority));
  const openTotal = priority.reduce((s, p) => s + p.open, 0);
  const points = cc?.closure_trend.points ?? [];
  const trend = points.map((p, i) => ({ label: fmtDayMonth(p.date) || String(i), value: p.closed }));
  const equity = cc?.equity;
  const districts = equity?.districts ?? [];
  const mapDistricts = gd.map((d) => ({ code: d.code, district: d.name, state: d.state ?? "", value: d.gaps_per_100, display: d.gaps_per_100 == null ? "None" : d.gaps_per_100.toFixed(1) }));
  // Failed background jobs and the activity feed describe the application's own backend: system administrators only.
  const isSystem = can("system.configure");
  const alerts = (cc?.alerts ?? []).filter((r) => isSystem || r.key !== "jobs_failed");
  const rules = (cc?.alert_rules ?? []).filter((r) => isSystem || r.key !== "jobs_failed");
  const sortedRules = [...rules].sort((a, b) => Number(b.status === "alert") - Number(a.status === "alert"));

  const withDensity = districts.filter((d) => per100(d) !== null);
  const densest = withDensity.length ? withDensity.reduce((a, b) => ((per100(b) ?? 0) > (per100(a) ?? 0) ? b : a)) : null;
  const highOpen = priority.find((p) => p.priority === "high")?.open ?? 0;
  const takeaways: string[] = [];
  if (scopeArea) {
    // A selected state or district: sentences come from its own geography figures.
    if (scopeArea.gaps_per_100 != null) takeaways.push(`${scopeArea.name} has ${scopeArea.gaps_per_100.toFixed(1)} care gap cases per 100 residents.`);
    if (scopeArea.open_care_gaps != null && scopeArea.population) takeaways.push(`${fmtInt(scopeArea.open_care_gaps)} care gaps are open among ${fmtInt(scopeArea.population)} residents.`);
    if (scopeArea.closure_rate != null) takeaways.push(`Closure rate is ${fmtPct(scopeArea.closure_rate)}.`);
    if (scopeArea.in_outreach) takeaways.push(`${fmtInt(scopeArea.in_outreach)} cases are in outreach.`);
  } else {
    if (densest) takeaways.push(`${densest.district_code} has the most care gap cases per resident (${(per100(densest) ?? 0).toFixed(1)} per 100).`);
    if (closure?.value != null && closure.numerator != null) takeaways.push(`Closure rate is ${fmtPct(closure.value)}: ${fmtInt(closure.numerator)} of ${fmtInt(closure.denominator)} cases are completed.`);
    if (highOpen > 0) takeaways.push(`${fmtInt(highOpen)} high priority care gaps are open.`);
    {
      const n = alerts.filter((a) => a.status === "alert").length;
      if (n > 0) takeaways.push(`${n} ${n === 1 ? "alert needs" : "alerts need"} attention.`);
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="Command Center"
        title="Population Health Overview"
        description="Prevent • Detect • Treat • Close the Loop"
        actions={
          <>
            <DataLabel kind="live" />
            {cc && <span className="text-[0.8125rem] text-ink-muted">Updated {fmtDateTime(cc.generated_at)}</span>}
            <button type="button" className={btnSecondary} onClick={res.reload} disabled={res.loading}>
              <Icon name="refresh" className="size-4" />
              Refresh
            </button>
          </>
        }
      />

      {res.error ? (
        isMissingEndpoint(res.error) ? (
          <Unavailable endpoint="GET /api/v1/analytics/command-center" />
        ) : (
          <ErrorPanel error={res.error} onRetry={res.reload} />
        )
      ) : (
        <>
          <div className="mb-6 grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-4 [&>*]:min-w-0">
            <KpiTile loading={loading} icon="users-round" label="Total population" value={total ? fmtInt(total.value) : null} basis={scopeArea ? scopeLabel : cc?.dataset.latest_version ? `Dataset ${cc.dataset.latest_version}` : undefined} />
            <KpiTile
              loading={loading}
              icon="target"
              label="Residents with care gaps"
              value={withGaps ? fmtInt(withGaps.value) : null}
              badge={total && withGaps && share(withGaps.value, total.value) ? <Pill tone="brand">{share(withGaps.value, total.value)}</Pill> : undefined}
              basis="Share of population"
            />
            <KpiTile
              loading={loading}
              icon="check-circle"
              label="Open care gaps"
              value={openGaps ? fmtInt(openGaps.value) : null}
              badge={openGaps && allGaps && share(openGaps.value, allGaps.value) ? <Pill tone="brand">{share(openGaps.value, allGaps.value)}</Pill> : undefined}
              delta={<Change cc={cc} kpiKey="care_gaps_open" note />}
            />
            <KpiTile
              loading={loading}
              icon="shield"
              label="Closure rate"
              value={closure ? fmtPct(closure.value) : null}
              badge={closed ? <Pill tone="neutral">{fmtInt(closed.value)} closed</Pill> : undefined}
              delta={<Change cc={cc} kpiKey="closure_rate" pp />}
            />
          </div>

          <div className="mb-6 grid gap-5 xl:grid-cols-[minmax(0,19rem)_minmax(0,1fr)_minmax(0,19rem)] [&>*]:min-w-0">
            <div className="flex flex-col gap-5 [&>*]:min-w-0">
              <ProgramsGlance query={scopeQuery} label={scopeArea ? scopeLabel : undefined} />
              <Card as="section" aria-labelledby="cc-priority">
                <CardHeader id="cc-priority" title="Care gaps by priority" subtitle="Open cases" aside={scopeArea ? <Pill tone="brand" icon="pin">{scopeLabel}</Pill> : undefined} />
                <div className="px-5 pb-5">
                  {loading ? (
                    <div className="skeleton h-28 w-full" role="status" aria-label="Loading priorities" />
                  ) : openTotal > 0 ? (
                    <ul aria-label="Open care gaps by priority" className="space-y-3.5">
                      {priority.map((p) => (
                        <li key={p.priority} className="flex items-center gap-3">
                          <span className="w-14 shrink-0 text-ink-soft">{PRIORITY_LABEL[p.priority] ?? humanize(p.priority)}</span>
                          <span className="h-3 min-w-8 flex-1 overflow-hidden rounded-full bg-neutral-bg" aria-hidden="true">
                            <span className="block h-full rounded-full" style={{ width: `${(p.open / openTotal) * 100}%`, background: p.priority === "high" ? "var(--scale-1)" : p.priority === "elevated" ? "var(--scale-3)" : "var(--scale-4)" }} />
                          </span>
                          <span className="tnum w-20 shrink-0 text-right font-semibold">
                            {fmtInt(p.open)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="py-6 text-center text-ink-muted">No open care gaps. Cases appear once approved rules are evaluated.</p>
                  )}
                </div>
              </Card>
              <CareJourneyCard query={scopeQuery} label={scopeArea ? scopeLabel : undefined} />
            </div>

            <div className="flex flex-col gap-5 [&>*]:min-w-0">
              <Card as="section" aria-labelledby="cc-density">
                <CardHeader id="cc-density" icon="map" title="Care gap density" subtitle="Care gap cases per 100 residents. Select a state to open it, then a district." />
                <div className="px-5 pb-5">
                  {loading || geo.loading ? (
                    <div className="skeleton h-[28rem] w-full" role="status" aria-label="Loading map" />
                  ) : cc?.limited || !equity ? (
                    <p className="py-12 text-center text-ink-muted">District detail is not available for your role.</p>
                  ) : mapDistricts.length ? (
                    <IndiaMap
                      districts={mapDistricts}
                      state={stateName}
                      district={scope || null}
                      valueLabel="care gap cases per 100 residents"
                      onState={(st) => {
                        setPickedState(st);
                        setScope("");
                      }}
                      onDistrict={(code) => setScope(code ?? "")}
                    />
                  ) : (
                    <div className="py-12 text-center">
                      <p className="text-ink-muted">Map data is unavailable right now.</p>
                      <button type="button" className={`${btnSecondary} mt-3`} onClick={geo.reload}>
                        Retry
                      </button>
                    </div>
                  )}
                </div>
              </Card>
              <Card as="section" aria-labelledby="cc-take">
                <CardHeader id="cc-take" icon="insight" title="Key takeaways" />
                <div className="px-5 pb-5">
                  {loading ? (
                    <div className="skeleton h-20 w-full" role="status" aria-label="Loading takeaways" />
                  ) : takeaways.length ? (
                    <ul className="space-y-2.5">
                      {takeaways.map((t) => (
                        <li key={t} className="flex items-start gap-2.5 text-ink-soft">
                          <span aria-hidden="true" className="mt-2 size-1.5 shrink-0 rounded-full bg-brand-mid" />
                          {t}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="text-ink-muted">Takeaways appear once care gap cases exist.</p>
                  )}
                </div>
              </Card>
            </div>

            <div className="flex flex-col gap-5 [&>*]:min-w-0">
              <Card as="section" aria-labelledby="cc-step1">
                <div className="flex items-start gap-3 px-5 pt-4">
                  <span aria-hidden="true" className="grid size-7 shrink-0 place-items-center rounded-full border border-line-strong text-sm font-semibold text-ink-soft">1</span>
                  <h2 id="cc-step1" className="font-display text-lg leading-7 font-bold">India overview</h2>
                </div>
                <div className="px-5 pt-2 pb-4">
                  <Figures area={geo.data?.country.area} />
                </div>
              </Card>
              <Card as="section" aria-labelledby="cc-step2">
                <div className="flex items-start gap-3 px-5 pt-4">
                  <span aria-hidden="true" className="grid size-7 shrink-0 place-items-center rounded-full border border-line-strong text-sm font-semibold text-ink-soft">2</span>
                  <h2 id="cc-step2" className="font-display text-lg leading-7 font-bold">State view</h2>
                </div>
                <div className="px-5 pt-2 pb-4">
                  {stateArea ? (
                    <>
                      <p className="mb-2 font-semibold">{stateArea.name}</p>
                      <Figures area={stateArea} />
                      <button type="button" className={`${btnSecondary} mt-3`} onClick={() => { setPickedState(null); setScope(""); }}>
                        Back to India
                      </button>
                    </>
                  ) : (
                    <p className="text-ink-muted">Select a state on the map to see its figures.</p>
                  )}
                </div>
              </Card>
              <Card as="section" aria-labelledby="cc-step3">
                <div className="flex items-start gap-3 px-5 pt-4">
                  <span aria-hidden="true" className="grid size-7 shrink-0 place-items-center rounded-full border border-line-strong text-sm font-semibold text-ink-soft">3</span>
                  <h2 id="cc-step3" className="font-display text-lg leading-7 font-bold">District view</h2>
                </div>
                <div className="px-5 pt-2 pb-4">
                  {chosenArea ? (
                    <>
                      <p className="mb-2 font-semibold">
                        {chosenArea.name}, {chosenArea.state}
                      </p>
                      <Figures area={chosenArea} />
                      <ProgrammeBars area={chosenArea} />
                      {can("residents.view") && (
                        <Link href={`/residents?district=${chosenArea.code}`} className={`${btnPrimary} mt-3`}>
                          View residents
                          <Icon name="arrow-right" className="size-4" />
                        </Link>
                      )}
                    </>
                  ) : (
                    <p className="text-ink-muted">Open a state, then select one of the shaded districts.</p>
                  )}
                </div>
              </Card>
            </div>
          </div>

          <div className="mb-6 grid gap-5 xl:grid-cols-3 [&>*]:min-w-0">
            <Card as="section" aria-labelledby="cc-closure">
                <CardHeader id="cc-closure" title="Closure trend" subtitle={cc ? `Cases closed per day, last ${cc.closure_trend.days} days` : undefined} aside={scopeArea ? <Pill tone="brand" icon="pin">{scopeLabel}</Pill> : undefined} />
                <div className="px-4 pb-4">
                  {loading ? (
                    <div className="skeleton h-48 w-full" role="status" aria-label="Loading closure trend" />
                  ) : trend.length ? (
                    <>
                      <div className="flex flex-wrap items-center justify-between gap-2 px-1">
                        <p className="tnum font-display text-3xl font-bold">
                          {closure ? fmtPct(closure.value) : "Not available"} <span className="font-sans text-sm font-medium text-ink-muted">closure rate</span>
                        </p>
                        <Pill tone="brand">
                          Latest day: {fmtInt(trend[trend.length - 1].value)} closed
                        </Pill>
                      </div>
                      <AreaTrend points={trend} height={170} ariaLabel="Care gaps closed per day" tableCaption="Care gaps closed per day" seriesLabel="Closed" yFormat={(n) => String(Math.round(n))} />
                    </>
                  ) : (
                    <p className="py-12 text-center text-ink-muted">No case activity in this window yet. Closed cases will appear here per day.</p>
                  )}
                </div>
              </Card>

            <Card as="section" aria-labelledby="cc-equity">
              <CardHeader id="cc-equity" icon="scale" title="Equity across districts" subtitle="Share of each district's care gaps that reached planning" aside={scopeArea ? <Pill tone="brand" icon="pin">{scopeLabel}</Pill> : undefined} />
              <div className="px-5 pb-5">
                {loading ? (
                  <div className="skeleton h-48 w-full" role="status" aria-label="Loading equity" />
                ) : cc?.limited || !equity ? (
                  <p className="py-8 text-center text-ink-muted">District equity is not available for your role.</p>
                ) : districts.some((d) => d.cases > 0) ? (
                  <div className="grid items-start gap-5">
                    <Columns
                      items={districts.map((d) => ({ label: d.district_code, value: d.coverage, display: d.coverage == null ? "None" : fmtPct(d.coverage) }))}
                      max={1}
                      ariaLabel="Planning coverage by district"
                      tableCaption="Planning coverage by district"
                      valueHeader="Coverage"
                    />
                    <dl className="border-line flex flex-wrap gap-x-10 gap-y-4 border-t pt-4">
                      <div>
                        <dt className="text-ink-muted">Coverage gap between districts</dt>
                        <dd className="tnum font-display text-3xl font-bold">{equity.coverage.gap == null ? "Not available" : `${(equity.coverage.gap * 100).toFixed(1)} pp`}</dd>
                        {equity.coverage.min && equity.coverage.max && (
                          <dd className="mt-1 max-w-56 text-xs text-ink-muted">
                            Lowest {equity.coverage.min.district_code} ({fmtPct(equity.coverage.min.value)}), highest {equity.coverage.max.district_code} ({fmtPct(equity.coverage.max.value)})
                          </dd>
                        )}
                      </div>
                      <div>
                        <dt className="text-ink-muted">Closure rate gap between districts</dt>
                        <dd className="tnum font-display text-3xl font-bold">{equity.closure_rate.gap == null ? "Not available" : `${(equity.closure_rate.gap * 100).toFixed(1)} pp`}</dd>
                      </div>
                    </dl>
                  </div>
                ) : (
                  <p className="py-8 text-center text-ink-muted">No care gap cases yet, so district coverage cannot be measured.</p>
                )}
              </div>
            </Card>

            <Card as="section" id="alerts" aria-labelledby="cc-alerts" className="scroll-mt-24">
              <CardHeader
                id="cc-alerts"
                title={
                  <>
                    Active alerts <span className="text-danger">({alerts.filter((a) => a.status === "alert").length})</span>
                  </>
                }
                subtitle="Threshold rules, each with its definition"
                aside={scopeArea ? <Pill tone="brand" icon="pin">{scopeLabel}</Pill> : undefined}
              />
              <div className="px-5 pb-4">
                {loading ? (
                  <div className="skeleton h-40 w-full" role="status" aria-label="Loading alerts" />
                ) : sortedRules.length ? (
                  <>
                    <ul className="divide-y divide-line">
                      {sortedRules.map((r) => (
                        <AlertRow key={r.key} r={r} />
                      ))}
                    </ul>
                    <details className="mt-1 text-[0.8125rem]">
                      <summary className="cursor-pointer font-medium text-brand-ink underline underline-offset-2">How alerts are defined</summary>
                      <dl className="mt-2 space-y-2 text-ink-muted">
                        {sortedRules.map((r) => (
                          <div key={r.key}>
                            <dt className="font-medium text-ink-soft">{r.title}</dt>
                            <dd>{displayText(r.definition)}</dd>
                          </div>
                        ))}
                      </dl>
                    </details>
                  </>
                ) : (
                  <p className="py-8 text-center text-ink-muted">No alert rules reported.</p>
                )}
              </div>
            </Card>
          </div>

          <div className="mb-6">
            <OperationsSnapshot outreach={outreach?.value} loading={loading} />
          </div>
          {can("analytics.view") && (
            <p className="mb-6">
              <Link href="/evaluation" className={btnSecondary}>
                View evaluation report
                <Icon name="arrow-right" className="size-4" />
              </Link>
            </p>
          )}

          {isSystem && cc?.recent_activity && (
            <Card as="section" aria-labelledby="cc-activity" className="mb-6">
              <CardHeader id="cc-activity" icon="layers" title="Recent activity" aside={<Link href="/audit" className="text-[0.8125rem] font-semibold text-brand-ink underline underline-offset-2">View audit log</Link>} />
              <div className="px-5 pb-4">
                {cc.recent_activity.items.length ? (
                  <ul className="divide-y divide-line">
                    {cc.recent_activity.items.slice(0, 6).map((a, i) => {
                      const bad = a.outcome === "failure" || a.outcome === "failed" || a.outcome === "denied";
                      return (
                        <li key={`${a.at}-${i}`} className="flex items-start gap-3 py-2.5">
                          <Icon name={bad ? "x-circle" : "check-circle"} className={`mt-0.5 size-[1.15rem] ${bad ? "text-danger" : "text-ok"}`} />
                          <div className="min-w-0 flex-1">
                            <p className="truncate font-medium">{humanize(a.title.replace(/\./g, " "))}</p>
                            <p className="text-xs text-ink-muted">
                              {a.kind === "job" ? `Job, ${humanize(a.outcome)}${a.total_items ? `, ${fmtInt(a.processed_items)} of ${fmtInt(a.total_items)} items` : ""}` : `${humanize(a.outcome)}${a.actor_role ? `, ${humanize(a.actor_role)}` : ""}`}
                            </p>
                          </div>
                          <time dateTime={a.at} className="tnum shrink-0 text-xs text-ink-muted">
                            {fmtDateTime(a.at)}
                          </time>
                        </li>
                      );
                    })}
                  </ul>
                ) : (
                  <p className="py-8 text-center text-ink-muted">No changes have been recorded yet.</p>
                )}
              </div>
            </Card>
          )}
        </>
      )}
    </>
  );
}
