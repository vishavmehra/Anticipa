"use client";

import { useState } from "react";
import { api, isMissingEndpoint } from "@/lib/api";
import { ageBandLabel, fmtDayMonth, fmtInt, fmtPct, humanize } from "@/lib/format";
import type { CcDistrict, CommandCenter, PopulationOverview } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { useRange, useScope } from "@/components/app-shell";
import { CompareNote, InfoTip, Pyramid, type PopulationExplorer } from "@/components/features";
import { DistrictMap } from "@/components/command-widgets";
import {
  AreaTrend,
  BarList,
  Card,
  CardHeader,
  CHART,
  Columns,
  DataLabel,
  Donut,
  ErrorPanel,
  FilterField,
  Icon,
  PageHeader,
  PillTabs,
  StackedBar,
  Unavailable,
  btnSecondary,
  selectClass,
  type IconName,
} from "@/components/ui";

type Tab = "population" | "needs" | "trends" | "equity";
const TABS = [
  { id: "population", label: "Population", icon: "users-round" },
  { id: "needs", label: "Needs and gaps", icon: "target" },
  { id: "trends", label: "Trends", icon: "trend-up" },
  { id: "equity", label: "Equity", icon: "scale" },
] as const satisfies readonly { id: Tab; label: string; icon: IconName }[];

type ViewBy = "density" | "closure" | "coverage" | "residents";
const VIEWS: Record<ViewBy, { label: string; value: (d: CcDistrict) => number | null; display: (v: number) => string; unit: string }> = {
  density: { label: "Care gap cases per 100 residents", value: (d) => (d.residents > 0 ? (d.cases / d.residents) * 100 : null), display: (v) => v.toFixed(1), unit: "per 100 residents" },
  closure: { label: "Closure rate", value: (d) => d.closure_rate, display: (v) => fmtPct(v), unit: "of cases closed" },
  coverage: { label: "Planning coverage", value: (d) => d.coverage, display: (v) => fmtPct(v), unit: "of cases planned" },
  residents: { label: "Residents", value: (d) => d.residents, display: (v) => fmtInt(Math.round(v)), unit: "residents" },
};
const PRIORITY_LABEL: Record<string, string> = { high: "High", elevated: "Medium", routine: "Low" };
const PRIORITY_COLOR: Record<string, string> = { high: "var(--scale-1)", elevated: "var(--scale-3)", routine: "var(--scale-4)" };
const none = (what: string) => <p className="py-10 text-center text-ink-muted">{what} is not available for your role.</p>;

export default function InsightsPage() {
  const { days } = useRange();
  const [tab, setTab] = useState<Tab>("population");
  const [view, setView] = useState<ViewBy>("density");
  const { scope } = useScope();
  const [demo, setDemo] = useState<"demographics" | "priority">("demographics");
  const [area, setArea] = useState("");
  const [band, setBand] = useState("");
  const [sexF, setSexF] = useState("");

  const cc = useApi(`insights-cc-${days}`, (signal) => api<CommandCenter>(`/analytics/command-center?compare=previous&days=${days}`, { signal }));
  const pop = useApi("insights-pop", (signal) => api<PopulationOverview>("/analytics/population-overview", { signal }));
  const c = cc.data?.data;
  const cmp = c?.comparison;
  const o = pop.data?.data;
  const district = area || scope;
  const exp = useApi(`insights-exp:${district}:${band}:${sexF}`, (signal) => api<PopulationExplorer>("/analytics/population-explorer", { query: { district: district || undefined, age_band: band || undefined, sex: sexF || undefined }, signal }));
  const ex = exp.data?.data;
  const slice = ex?.available ? ex.slice : null;
  const olderBands = (ex?.options.age_bands ?? []).filter((b) => parseInt(b, 10) >= 50);
  const loading = cc.loading && !c;
  const kp = (key: string) => c?.kpis.find((k) => k.key === key)?.value ?? null;
  const districts = c?.equity?.districts ?? [];
  const V = VIEWS[view];
  const sel = districts.find((d) => d.district_code === scope) ?? null;
  const points = c?.closure_trend.points ?? [];
  const priority = [...(c?.gaps_by_priority.items ?? [])].sort((a, b) => ["high", "elevated", "routine"].indexOf(a.priority) - ["high", "elevated", "routine"].indexOf(b.priority));
  const openTotal = priority.reduce((s, p) => s + p.open, 0);
  const age = o?.distributions.find((d) => d.key === "age_band");
  const sex = o?.distributions.find((d) => d.key === "sex");
  const total = kp("residents_total");
  const withGaps = kp("residents_with_open_gaps");

  // Plain-language findings, each computed from the figures on this page.
  const ranked = districts.map((d) => ({ d, v: VIEWS.density.value(d) })).filter((x): x is { d: CcDistrict; v: number } => x.v !== null).sort((a, b) => b.v - a.v);
  const insights: { icon: IconName; title: string; text: string }[] = [];
  if (ranked.length > 1) insights.push({ icon: "users-round", title: `Highest care gap density in ${ranked[0].d.district_code}`, text: `${ranked[0].v.toFixed(1)} cases per 100 residents, against ${ranked[ranked.length - 1].v.toFixed(1)} in ${ranked[ranked.length - 1].d.district_code}.` });
  if (openTotal > 0) {
    const high = priority.find((p) => p.priority === "high")?.open ?? 0;
    insights.push({ icon: "trend-up", title: `${fmtInt(high)} high priority care gaps are open`, text: `${((high / openTotal) * 100).toFixed(0)}% of the ${fmtInt(openTotal)} open care gaps.` });
  }
  const dueRanked = districts.filter((d) => d.due_share != null).sort((x, y) => (y.due_share ?? 0) - (x.due_share ?? 0));
  if (dueRanked.length > 1) {
    const dueAll = dueRanked.reduce((s, d) => s + (d.due_residents ?? 0), 0);
    insights.push({ icon: "target", title: `${fmtInt(dueAll)} residents are due for care`, text: `The largest share is in ${dueRanked[0].district_code} (${fmtPct(dueRanked[0].due_share ?? null)}), the smallest in ${dueRanked[dueRanked.length - 1].district_code} (${fmtPct(dueRanked[dueRanked.length - 1].due_share ?? null)}).` });
  }
  if (c?.equity?.coverage.gap != null && c.equity.coverage.min && c.equity.coverage.max)
    insights.push({ icon: "scale", title: `Planning coverage differs by ${(c.equity.coverage.gap * 100).toFixed(1)} percentage points`, text: `Lowest in ${c.equity.coverage.min.district_code} (${fmtPct(c.equity.coverage.min.value)}), highest in ${c.equity.coverage.max.district_code} (${fmtPct(c.equity.coverage.max.value)}).` });

  const err = cc.error ?? pop.error;
  return (
    <>
      <PageHeader
        eyebrow="Population"
        title="Population Insights"
        description="Explore population, health needs and preventive care opportunities."
        tabs={<PillTabs idBase="insights" label="Insight views" items={TABS} value={tab} onChange={setTab} />}
        actions={<DataLabel kind="live" />}
      />

      {err ? (
        isMissingEndpoint(err) ? <Unavailable endpoint="GET /api/v1/analytics/command-center" /> : <ErrorPanel error={err} onRetry={() => { cc.reload(); pop.reload(); }} />
      ) : (
        <div role="tabpanel" id="insights-panel" aria-labelledby={`insights-tab-${tab}`}>
          <div className="glass mb-5 grid grid-cols-1 gap-y-3 px-5 py-4 sm:grid-cols-3 [&>*]:min-w-0">
            {[
              ["Total population", total, "users-round", null],
              ["Residents with open care gaps", withGaps, "target", null],
              ["Open care gaps", kp("care_gaps_open"), "layers", "care_gaps_open"],
            ].map(([label, v, icon, ck], i) => (
              <div key={label as string} className={`flex items-center gap-3 ${i ? "sm:border-l sm:border-line sm:pl-5" : ""}`}>
                <span className="grid size-10 shrink-0 place-items-center icon-tile">
                  <Icon name={icon as IconName} className="size-5" />
                </span>
                <div>
                  <p className="tnum font-display text-2xl leading-7 font-bold">{loading ? "…" : fmtInt(v as number | null)}</p>
                  <p className="text-[0.8125rem] text-ink-muted">{label as string}</p>
                  {ck ? <CompareNote cmp={cmp} k={ck as string} /> : null}
                </div>
              </div>
            ))}
          </div>

          {tab === "population" && (
            <div className="mb-5 grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)] [&>*]:min-w-0">
              <Card as="section" aria-labelledby="ins-map">
                <CardHeader
                  id="ins-map"
                  icon="map"
                  title="District map"
                  subtitle="Scroll up over a state to open it, then select a district."
                  aside={
                    <FilterField label="View by" className="min-w-56">
                      <select value={view} onChange={(e) => setView(e.target.value as ViewBy)} className={selectClass}>
                        {(Object.keys(VIEWS) as ViewBy[]).map((k) => (
                          <option key={k} value={k}>
                            {VIEWS[k].label}
                          </option>
                        ))}
                      </select>
                    </FilterField>
                  }
                />
                <div className="px-5 pb-5">
                  {loading ? (
                    <div className="skeleton h-80 w-full" role="status" aria-label="Loading districts" />
                  ) : c?.limited || !c?.equity ? (
                    none("District detail")
                  ) : districts.length ? (
                    <>
                      <DistrictMap districts={districts} value={V.value} display={V.display} label={V.label} />
                      {sel && (
                        <dl className="tile mt-4 grid grid-cols-2 gap-x-6 gap-y-2 p-4 sm:grid-cols-4">
                          <div className="col-span-2 sm:col-span-4">
                            <dt className="sr-only">District</dt>
                            <dd className="font-display text-lg font-bold">District {sel.district_code}</dd>
                          </div>
                          {[
                            ["Residents", fmtInt(sel.residents)],
                            ["Care gap cases", fmtInt(sel.cases)],
                            ["Planned", fmtInt(sel.planned)],
                            ["Completed", fmtInt(sel.completed)],
                          ].map(([k, v]) => (
                            <div key={k}>
                              <dt className="text-xs text-ink-muted">{k}</dt>
                              <dd className="tnum font-semibold whitespace-nowrap">{v}</dd>
                            </div>
                          ))}
                        </dl>
                      )}
                    </>
                  ) : (
                    <p className="py-10 text-center text-ink-muted">No districts reported.</p>
                  )}
                </div>
              </Card>

              <Card as="section" aria-labelledby="ins-demo">
                <CardHeader
                  id="ins-demo"
                  icon="users"
                  title={demo === "demographics" ? "Age and sex distribution" : "Open care gaps by priority"}
                  aside={
                    <PillTabs
                      idBase="ins-demo"
                      label="Distribution view"
                      value={demo}
                      onChange={setDemo}
                      items={[
                        { id: "demographics", label: "Demographics" },
                        { id: "priority", label: "Priority" },
                      ]}
                    />
                  }
                />
                <div role="tabpanel" id="ins-demo-panel" aria-labelledby={`ins-demo-tab-${demo}`} className="px-5 pb-5">
                  {demo === "demographics" ? (
                    ex?.available ? (
                      <div className="space-y-3">
                        <p className="text-[0.8125rem] text-ink-muted">
                          {district ? `District ${district}. ` : "All districts. "}Select an age band to filter the explorer below.
                        </p>
                        <Pyramid cells={ex.pyramid} bands={ex.options.age_bands} band={band} onBand={setBand} />
                      </div>
                    ) : !age || !sex ? (
                      <div className="skeleton h-56 w-full" role="status" aria-label="Loading demographics" />
                    ) : (
                      <div className="space-y-5">
                        <BarList
                          ariaLabel="Residents by age band"
                          tableCaption="Residents by age band"
                          valueHeader="Residents"
                          labelWidth="w-20"
                          max={Math.max(...age.buckets.map((b) => b.count), 1)}
                          rows={age.buckets.map((b) => ({ label: ageBandLabel(b.key), value: b.count, display: fmtInt(b.count), note: `${(b.share * 100).toFixed(1)}%` }))}
                        />
                        <StackedBar ariaLabel="Residents by sex" tableCaption="Residents by sex" segments={sex.buckets.map((b, i) => ({ label: humanize(b.key), value: b.count, color: i ? CHART.light : CHART.violet }))} />
                      </div>
                    )
                  ) : openTotal > 0 ? (
                    <Donut
                      slices={priority.map((p) => ({ label: PRIORITY_LABEL[p.priority] ?? humanize(p.priority), value: p.open, color: PRIORITY_COLOR[p.priority] ?? CHART.navy }))}
                      centerValue={fmtInt(openTotal)}
                      centerLabel="open care gaps"
                      ariaLabel={`Open care gaps by priority, ${openTotal} in total`}
                      tableCaption="Open care gaps by priority"
                    />
                  ) : (
                    <p className="py-10 text-center text-ink-muted">No open care gaps.</p>
                  )}
                </div>
              </Card>
            </div>
          )}

          {tab === "needs" && (
            <div className="mb-5 grid gap-4 xl:grid-cols-2 [&>*]:min-w-0">
              <Card as="section" aria-labelledby="ins-prio">
                <CardHeader id="ins-prio" icon="target" title="Open care gaps by priority" />
                <div className="px-5 pb-5">
                  {openTotal > 0 ? (
                    <Donut
                      slices={priority.map((p) => ({ label: PRIORITY_LABEL[p.priority] ?? humanize(p.priority), value: p.open, color: PRIORITY_COLOR[p.priority] ?? CHART.navy }))}
                      centerValue={fmtInt(openTotal)}
                      centerLabel="open care gaps"
                      ariaLabel={`Open care gaps by priority, ${openTotal} in total`}
                      tableCaption="Open care gaps by priority"
                    />
                  ) : (
                    <p className="py-10 text-center text-ink-muted">{loading ? "Loading" : "No open care gaps."}</p>
                  )}
                </div>
              </Card>
              <Card as="section" aria-labelledby="ins-cases">
                <CardHeader id="ins-cases" icon="layers" title="Care gap cases by district" />
                <div className="px-5 pb-5">
                  {c?.limited || !c?.equity ? (
                    none("District detail")
                  ) : (
                    <BarList
                      ariaLabel="Care gap cases by district"
                      tableCaption="Care gap cases by district"
                      valueHeader="Cases"
                      labelWidth="w-14"
                      rows={districts.map((d) => ({ label: d.district_code, value: d.cases, display: fmtInt(d.cases), note: `${fmtInt(d.completed)} closed` }))}
                    />
                  )}
                </div>
              </Card>
            </div>
          )}

          {tab === "trends" && (
            <div className="mb-5 grid gap-4 xl:grid-cols-2 [&>*]:min-w-0">
              {(
                [
                  ["Cases closed per day", "closed", "ins-closed"],
                  ["Cases opened per day", "opened", "ins-opened"],
                ] as const
              ).map(([title, key, id]) => (
                <Card as="section" key={id} aria-labelledby={id}>
                  <CardHeader id={id} icon="trend-up" title={title} subtitle={c ? `Last ${c.closure_trend.days} days` : undefined} />
                  <div className="px-5 pb-5">
                    {points.length ? (
                      <AreaTrend points={points.map((p, i) => ({ label: fmtDayMonth(p.date) || String(i), value: p[key] }))} ariaLabel={title} tableCaption={title} seriesLabel={key === "closed" ? "Closed" : "Opened"} yFormat={(n) => String(Math.round(n))} />
                    ) : (
                      <p className="py-10 text-center text-ink-muted">{loading ? "Loading" : "No case activity in this window yet."}</p>
                    )}
                  </div>
                </Card>
              ))}
            </div>
          )}

          {tab === "equity" && (
            <Card as="section" aria-labelledby="ins-equity" className="mb-5">
              <CardHeader id="ins-equity" icon="scale" title="Equity across districts" subtitle="Planning coverage and closure rate for each district" />
              <div className="px-5 pb-5">
                {c?.limited || !c?.equity ? (
                  none("District equity")
                ) : (
                  <div className="grid gap-6 lg:grid-cols-2 [&>*]:min-w-0">
                    <div>
                      <h3 className="mb-2 font-semibold">Planning coverage</h3>
                      <Columns items={districts.map((d) => ({ label: d.district_code, value: d.coverage, display: d.coverage == null ? "None" : fmtPct(d.coverage) }))} max={1} ariaLabel="Planning coverage by district" tableCaption="Planning coverage by district" valueHeader="Coverage" />
                      <p className="mt-2 text-ink-muted">Gap between districts: <span className="tnum font-semibold text-ink">{c.equity.coverage.gap == null ? "Not available" : `${(c.equity.coverage.gap * 100).toFixed(1)} pp`}</span></p>
                    </div>
                    <div>
                      <h3 className="mb-2 font-semibold">Closure rate</h3>
                      <Columns items={districts.map((d) => ({ label: d.district_code, value: d.closure_rate, display: d.closure_rate == null ? "None" : fmtPct(d.closure_rate) }))} ariaLabel="Closure rate by district" tableCaption="Closure rate by district" valueHeader="Closure rate" />
                      <p className="mt-2 text-ink-muted">Gap between districts: <span className="tnum font-semibold text-ink">{c.equity.closure_rate.gap == null ? "Not available" : `${(c.equity.closure_rate.gap * 100).toFixed(1)} pp`}</span></p>
                    </div>
                  </div>
                )}
              </div>
            </Card>
          )}

          <div className="mb-5 grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)] [&>*]:min-w-0">
            <Card as="section" aria-labelledby="ins-explorer">
              <CardHeader id="ins-explorer" icon="search" title="Population explorer" subtitle="Slice the population by age, sex and district." aside={<DataLabel kind="live" />} />
              <div className="grid gap-6 px-5 pb-5 md:grid-cols-[13rem_1fr]">
                <div className="space-y-3">
                  <FilterField label="Age group">
                    <select value={band} onChange={(e) => setBand(e.target.value)} className={selectClass}>
                      <option value="">All ages</option>
                      {olderBands.length > 1 && <option value={olderBands.join(",")}>Ages 50 and over</option>}
                      {(ex?.options.age_bands ?? []).map((b) => (
                        <option key={b} value={b}>
                          {ageBandLabel(b)}
                        </option>
                      ))}
                    </select>
                  </FilterField>
                  <FilterField label="Sex">
                    <select value={sexF} onChange={(e) => setSexF(e.target.value)} className={selectClass}>
                      <option value="">All</option>
                      {(ex?.options.sexes ?? ["female", "male"]).map((s) => (
                        <option key={s} value={s}>
                          {humanize(s)}
                        </option>
                      ))}
                    </select>
                  </FilterField>
                  <FilterField label="Area">
                    <select value={district} onChange={(e) => setArea(e.target.value)} className={selectClass}>
                      <option value="">All districts</option>
                      {(ex?.options.districts ?? districts.map((d) => ({ code: d.district_code, name: d.district_code, state: "" }))).map((d) => (
                        <option key={d.code} value={d.code}>
                          {d.name === d.code ? d.code : `${d.name} (${d.code})`}
                        </option>
                      ))}
                    </select>
                  </FilterField>
                  {(band || sexF || area) && (
                    <button type="button" className={btnSecondary} onClick={() => { setBand(""); setSexF(""); setArea(""); }}>
                      Clear filters
                    </button>
                  )}
                </div>
                {exp.error ? (
                  <ErrorPanel error={exp.error} onRetry={exp.reload} title="Could not load the explorer" />
                ) : !ex ? (
                  <div className="skeleton h-48" role="status" aria-label="Loading the explorer" />
                ) : !slice ? (
                  <p className="self-center text-ink-muted">{ex.reason ?? "The explorer has no figures yet."}</p>
                ) : (
                  <div className="grid items-center gap-5 lg:grid-cols-[minmax(0,14rem)_1fr]">
                    <Donut
                      slices={[
                        { label: "Due for care", value: slice.due_residents, color: CHART.violet },
                        { label: "Not due", value: Math.max(0, slice.population - slice.due_residents), color: CHART.light },
                      ]}
                      centerValue={fmtInt(slice.population)}
                      centerLabel="residents"
                      ariaLabel={`${fmtInt(slice.due_residents)} of ${fmtInt(slice.population)} residents are due for care`}
                      tableCaption="Residents due for care in the selected group"
                    />
                    <dl className="space-y-2">
                      {(
                        [
                          ["due_residents", "Due for care", `${fmtInt(slice.due_residents)} (${slice.population ? ((slice.due_residents / slice.population) * 100).toFixed(1) : "0"}%)`],
                          ["open_care_gaps", "Open care gaps", fmtInt(slice.open_care_gaps)],
                          ["gaps_per_100", "Care gaps per 100 residents", slice.gaps_per_100 == null ? "Not available" : slice.gaps_per_100.toFixed(1)],
                          ["closure_rate", "Closure rate", fmtPct(slice.closure_rate)],
                          ["coverage", "Planning coverage", fmtPct(slice.coverage)],
                        ] as const
                      ).map(([k, label, v]) => (
                        <div key={k} className="flex items-center justify-between gap-3 border-b border-line pb-1.5 last:border-0">
                          <dt className="text-ink-soft">
                            {label} {ex.definitions[k] && <InfoTip text={ex.definitions[k].definition} label={`What ${label} means`} />}
                          </dt>
                          <dd className="tnum font-semibold whitespace-nowrap">{v}</dd>
                        </div>
                      ))}
                    </dl>
                  </div>
                )}
              </div>
            </Card>

            <Card as="section" aria-labelledby="ins-key">
              <CardHeader id="ins-key" icon="insight" title="Key population insights" />
              <ul className="divide-y divide-line px-5 pb-3">
                {insights.length ? (
                  insights.map((i) => (
                    <li key={i.title} className="flex items-start gap-3 py-3">
                      <span className="grid size-10 shrink-0 place-items-center icon-tile">
                        <Icon name={i.icon} className="size-5" />
                      </span>
                      <div className="min-w-0">
                        <p className="font-semibold">{i.title}</p>
                        <p className="text-[0.8125rem] text-ink-muted">{i.text}</p>
                      </div>
                    </li>
                  ))
                ) : (
                  <li className="py-6 text-center text-ink-muted">{loading ? "Loading" : "Insights appear once care gap cases exist."}</li>
                )}
              </ul>
            </Card>
          </div>

          <button type="button" className={btnSecondary} onClick={() => { cc.reload(); pop.reload(); }} disabled={cc.loading}>
            <Icon name="refresh" className="size-4" />
            Refresh
          </button>
        </div>
      )}
    </>
  );
}
