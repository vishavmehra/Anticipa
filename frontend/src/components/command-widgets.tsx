"use client";

// Widgets shared by the Command Center and Population pages. Real data only:
//  - ProgramsGlance: GET /guidelines (approved care activities) + GET /care-gaps counts (page_size=1, meta.total).
//  - OperationsSnapshot: command-center and population-overview figures for everyone; pipeline and AI spend from
//    GET /analytics/processing for system administrators only.

import { useState } from "react";
import { api } from "@/lib/api";
import { useDistrictMap } from "@/lib/geography";
import type { CcDistrict } from "@/lib/types";
import { IndiaMap } from "./india-map";
import { fmtInt, fmtPct } from "@/lib/format";
import type { PopulationOverview, ProgrammesResponse } from "@/lib/types";
import type { Processing } from "@/components/features/payloads";
import { useApi } from "@/lib/use-api";
import { useCan, useScope } from "./app-shell";
import { Card, CardHeader, Icon, Pill, btnSecondary, type IconName } from "./ui";

const iconFor = (activity: string): IconName => (/cancer|screen|mammo|cervi|colo/.test(activity) ? "ribbon" : /vaccin|immun/.test(activity) ? "syringe" : /diabet/.test(activity) ? "droplet" : "heart");

/** One bar per programme (care activity): the share of its care gap cases that are completed. GET /analytics/programmes. */
export function ProgramsGlance({ query = "", label }: { query?: string; label?: string }) {
  const res = useApi(`glance-programmes${query}`, (signal) => api<ProgrammesResponse>(`/analytics/programmes${query ? `?${query}` : ""}`, { signal }));
  const rows = res.data?.data.programmes.slice(0, 6) ?? null;
  return (
    <Card as="section" aria-labelledby="glance-programs">
      <CardHeader id="glance-programs" title="Programs at a glance" subtitle="Share of each program's care gaps completed" aside={label ? <Pill tone="brand" icon="pin">{label}</Pill> : undefined} />
      <div className="px-5 pb-5">
        {res.error ? (
          <p className="py-6 text-center text-ink-muted">Programs are not available for your role.</p>
        ) : !rows ? (
          <div className="skeleton h-40 w-full" role="status" aria-label="Loading programs" />
        ) : rows.length === 0 ? (
          <p className="py-6 text-center text-ink-muted">No approved guideline has raised care gaps yet.</p>
        ) : (
          <ul aria-label="Share of care gaps completed by program" className="space-y-3.5">
            {rows.map((p) => (
              <li key={p.care_activity} className="flex items-center gap-3">
                <Icon name={iconFor(p.care_activity)} className="size-[1.1rem] text-brand-ink" />
                <span className="min-w-0 flex-1 text-[0.8125rem] leading-tight text-ink-soft">{p.label}</span>
                <span className="h-2.5 w-16 shrink-0 overflow-hidden rounded-full bg-neutral-bg" aria-hidden="true">
                  <span className="block h-full rounded-full" style={{ width: `${(p.closure_rate ?? 0) * 100}%`, background: "linear-gradient(90deg, var(--chart-violet), var(--chart-mid))" }} />
                </span>
                <span className="tnum w-12 shrink-0 text-right font-semibold">{p.closure_rate === null ? "None" : fmtPct(p.closure_rate)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

/** Bottom strip of the Command Center. Items that describe the backend (pipeline, AI spend) exist only for system administrators. */
export function OperationsSnapshot({ outreach, loading }: { outreach: number | null | undefined; loading: boolean }) {
  const can = useCan();
  const isSystem = can("system.configure");
  const pop = useApi("snap-pop", (signal) => api<PopulationOverview>("/analytics/population-overview", { signal }));
  const proc = useApi(isSystem ? "snap-proc" : "snap-proc-off", async (signal) => (isSystem ? (await api<Processing>("/analytics/processing", { signal })).data : null));
  const docs = pop.data?.data.metrics.find((m) => m.key === "documents_total")?.value;
  const pipe = proc.data?.pipeline.available ? proc.data.pipeline.ingestion.percent : null;
  const spend = proc.data?.ai_spend.total_usd;
  const items: { icon: IconName; label: string; value: string }[] = [
    ...(isSystem ? [{ icon: "gauge" as IconName, label: "Pipeline complete", value: pipe == null ? "Not available" : `${pipe.toFixed(1)}%` }] : []),
    { icon: "file", label: "Documents registered", value: docs == null ? (pop.loading ? "…" : "Not available") : fmtInt(docs) },
    ...(isSystem ? [{ icon: "cpu" as IconName, label: "AI spend", value: spend == null ? "Not available" : `$${spend.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` }] : []),
    { icon: "calendar" as IconName, label: "Cases in outreach", value: loading ? "…" : fmtInt(outreach) },
  ];
  return (
    <Card as="section" aria-labelledby="ops-snapshot">
      <div className="flex flex-wrap items-center gap-x-8 gap-y-4 px-6 py-5">
        <div className="flex items-center gap-3">
          <span className="grid size-11 place-items-center icon-tile">
            <Icon name="layers" className="size-5" />
          </span>
          <div>
            <h2 id="ops-snapshot" className="font-display text-lg leading-6 font-bold">
              Operations snapshot
            </h2>
            <p className="text-xs text-ink-muted">Pipeline, engagement and evidence</p>
          </div>
        </div>
        <div className="grid flex-1 grid-cols-2 gap-x-6 gap-y-4 lg:grid-cols-4 [&>*]:min-w-0">
          {items.map((it) => (
            <div key={it.label} className="flex items-center gap-3 lg:border-l lg:border-line lg:pl-6">
              <Icon name={it.icon} className="size-5 shrink-0 text-brand-ink" />
              <div className="min-w-0">
                <p className="tnum font-display text-xl leading-6 font-bold">{it.value}</p>
                <p className="text-xs text-ink-muted">{it.label}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}

/** India map for any page: states, then the districts of a state. Selecting a district sets the top bar scope. */
export function DistrictMap({ districts, value, display, label }: { districts: readonly CcDistrict[]; value: (d: CcDistrict) => number | null; display: (v: number) => string; label: string }) {
  const { scope, setScope } = useScope();
  const dmap = useDistrictMap();
  const [pickedState, setPickedState] = useState<string | null>(null);
  const meta = dmap.data ?? [];
  const stateName = scope ? (meta.find((m) => m.code === scope)?.state ?? null) : pickedState;
  const list = districts.flatMap((d) => {
    const m = meta.find((x) => x.code === d.district_code);
    const v = value(d);
    return m ? [{ code: d.district_code, district: m.district, state: m.state, value: v, display: v === null ? "None" : display(v) }] : [];
  });
  if (dmap.loading) return <div className="skeleton h-[28rem] w-full" role="status" aria-label="Loading map" />;
  if (!list.length)
    return (
      <div className="py-12 text-center">
        <p className="text-ink-muted">Map data is unavailable right now.</p>
        <button type="button" className={`${btnSecondary} mt-3`} onClick={dmap.reload}>
          Retry
        </button>
      </div>
    );
  return (
    <IndiaMap
      districts={list}
      state={stateName}
      district={scope || null}
      valueLabel={label}
      onState={(st) => {
        setPickedState(st);
        setScope("");
      }}
      onDistrict={(code) => setScope(code ?? "")}
    />
  );
}
