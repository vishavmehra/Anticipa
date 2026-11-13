"use client";

// Operations and capacity: the care network (clinics by district with seat use), the appointment schedule (real capacity
// slots of one clinic and day: booked of available seats), headline capacity figures, capacity by district, a rebalancing
// view and operational alerts. Real data only: /planning/clinics, /planning/capacity-slots, /analytics/capacity,
// /analytics/engagement, /analytics/command-center. Clinic types, a geographic map and a rebalance action have no backing
// data or endpoint (see data/reports/ui-data-gaps.json), so the network is drawn as a schematic and rebalancing is advice
// only. Route: /operations. Purpose: show judges capacity and bookings across the network.

import Link from "next/link";
import { useMemo, useState } from "react";
import { apiList } from "@/lib/api";
import { fmtDate, fmtInt, humanize } from "@/lib/format";
import type { Clinic } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { StatusBadge } from "@/components/status-badge";
import { DataTable, type Column } from "@/components/data-table";
import { Card, CardHeader, DataLabel, ErrorPanel, KpiTile, PageHeader, Pill, selectClass, Icon } from "@/components/ui";
import { GrowBar } from "./interactive";
import { PillTabs, TabPanel, useCapacity, useCommandCenter, useEngagementOverview, type PillTab } from "./kit";
import { fmtShare, plain } from "./shared";

type Tab = "scheduling" | "resources" | "rebalancing" | "network";
const TABS: PillTab<Tab>[] = [
  { key: "scheduling", label: "Scheduling", icon: "calendar" },
  { key: "resources", label: "Resource view", icon: "users" },
  { key: "rebalancing", label: "Rebalancing", icon: "refresh" },
  { key: "network", label: "Site network", icon: "pin" },
];

interface Slot {
  id: string;
  clinic_key: string;
  service_key: string;
  start_time: string;
  end_time: string;
  available_capacity: number;
  reserved_capacity: number;
  remaining_capacity: number;
  slot_status: string;
}

const tone = (u: number | null) => (u == null ? "neutral" : u >= 0.9 ? "danger" : u >= 0.75 ? "warn" : "ok");
const dayOf = (iso: string) => iso.slice(0, 10);
const hhmm = (iso: string) => iso.slice(11, 16);
const mins = (iso: string) => Number(iso.slice(11, 13)) * 60 + Number(iso.slice(14, 16));
const SVC_COLORS = ["#7b5bd6", "#3b82c4", "#3f9a5b", "#c98a1b"];

// ---------------------------------------------------------------- network map
function hash(s: string) {
  let h = 2166136261;
  for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return (h >>> 0) / 4294967295;
}

function NetworkMap({ clinics, util, selected, onSelect }: { clinics: Clinic[]; util: Record<string, number | null>; selected: string; onSelect: (d: string) => void }) {
  const districts = [...new Set(clinics.map((c) => c.district_code))].sort();
  const W = 520;
  const H = 300;
  const pos = (d: string, i: number) => {
    const a = (i / Math.max(1, districts.length)) * Math.PI * 2 - Math.PI / 2 + hash(d) * 0.3;
    return { x: W / 2 + Math.cos(a) * 190, y: H / 2 + Math.sin(a) * 105 };
  };
  return (
    <>
      <svg viewBox={`0 0 ${W} ${H}`} role="group" aria-label="Care network map. Each district label filters the page." className="block w-full">
        <defs>
          <radialGradient id="mapglow" cx="50%" cy="50%" r="60%">
            <stop offset="0%" stopColor="#efe9fc" />
            <stop offset="100%" stopColor="#faf8ff" />
          </radialGradient>
        </defs>
        <rect width={W} height={H} rx="22" fill="url(#mapglow)" />
        {districts.map((d, i) => {
          const p = pos(d, i);
          return <path key={`l-${d}`} d={`M${W / 2} ${H / 2}Q${(W / 2 + p.x) / 2 + 10} ${(H / 2 + p.y) / 2 - 14} ${p.x} ${p.y}`} stroke="#cbbdf0" strokeWidth="2" strokeDasharray="5 5" fill="none" />;
        })}
        <circle cx={W / 2} cy={H / 2} r="30" fill="#7b5bd6" />
        <text x={W / 2} y={H / 2 + 4} textAnchor="middle" fontSize="10" fontWeight="700" fill="#fff">Network</text>
        {districts.map((d, i) => {
          const p = pos(d, i);
          const here = clinics.filter((c) => c.district_code === d);
          const u = util[d] ?? null;
          const fill = u == null ? "#556579" : u >= 0.9 ? "#b42318" : u >= 0.75 ? "#7a4a05" : "#05603a";
          return (
            <g key={d} role="button" tabIndex={0} aria-pressed={selected === d} aria-label={`${d} ${u == null ? "No data" : fmtShare(u)}. Filter the page to district ${d}`} onClick={() => onSelect(d)} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onSelect(d))} style={{ cursor: "pointer", outline: "none" }} className="focus-visible:[&>rect]:stroke-[3] focus-visible:[&>rect]:stroke-[#5b3db5]">
              {here.slice(0, 8).map((c, k) => (
                <circle key={c.id} cx={p.x - (Math.min(here.length, 8) - 1) * 7 + k * 14} cy={p.y + 26} r="5" fill={c.operating_status === "open" ? "#7b5bd6" : "#fff"} stroke="#7b5bd6" strokeWidth="1.6" />
              ))}
              <rect x={p.x - 44} y={p.y - 15} width="88" height="30" rx="15" fill={selected === d ? "#efe9fc" : "#fff"} stroke={selected === d ? "#7b5bd6" : "#d9cdf5"} strokeWidth={selected === d ? 2.5 : 1} />
              <text x={p.x} y={p.y + 4} textAnchor="middle" fontSize="12" fontWeight="700" fill="#0f1b2d">
                {d}{" "}
                <tspan fill={fill}>{u == null ? "No data" : fmtShare(u)}</tspan>
              </text>
            </g>
          );
        })}
      </svg>
      <ul aria-label="Districts and seat use" className="sr-only">
        {districts.map((d) => (
          <li key={d}>
            {d}: {util[d] == null ? "no seat data" : `${fmtShare(util[d])} of seats booked`}
          </li>
        ))}
      </ul>
    </>
  );
}

function NetworkCard({ clinics, util, loading, selected, onSelect }: { clinics: Clinic[]; util: Record<string, number | null>; loading: boolean; selected: string; onSelect: (d: string) => void }) {
  const districts = new Set(clinics.map((c) => c.district_code));
  const open = clinics.filter((c) => c.operating_status === "open").length;
  return (
    <Card as="section" aria-labelledby="net-h" className="glass-strong">
      <CardHeader id="net-h" icon="pin" title="Care network" subtitle={loading ? "Loading clinics." : `${fmtInt(clinics.length)} clinics in ${districts.size} districts, ${fmtInt(open)} open. Dots are clinics, labels show seats booked. Select a district to filter.`} aside={<DataLabel kind="live" />} />
      <div className="px-4 pb-4">{loading ? <div className="skeleton h-64" /> : clinics.length === 0 ? <p className="px-1 text-ink-muted">No clinics are registered.</p> : <NetworkMap clinics={clinics} util={util} selected={selected} onSelect={onSelect} />}</div>
    </Card>
  );
}

// ---------------------------------------------------------------- schedule
function Schedule({ clinics }: { clinics: Clinic[] }) {
  const [clinicKey, setClinicKey] = useState<string>("");
  const [day, setDay] = useState<string | null>(null);
  const [view, setView] = useState<"day" | "week">("day");
  const key = clinicKey || clinics[0]?.clinic_key || "";
  const [now] = useState(() => Date.now()); // read once: a render must not call the clock
  const from = new Date(now).toISOString().slice(0, 10);
  const to = new Date(now + 14 * 86400000).toISOString().slice(0, 10);
  const res = useApi(`ops-slots:${key}`, (s) => (key ? apiList<Slot>("/planning/capacity-slots", { clinic_key: key, from, to, page_size: 100 }, s) : Promise.resolve(null)));
  const slots = useMemo(() => res.data?.data ?? [], [res.data]);
  const days = useMemo(() => [...new Set(slots.map((s) => dayOf(s.start_time)))].sort(), [slots]);
  const cur = day && days.includes(day) ? day : (days[0] ?? null);
  const idx = cur ? days.indexOf(cur) : -1;
  const today = slots.filter((s) => dayOf(s.start_time) === cur);
  const services = [...new Set(today.map((s) => s.service_key))].slice(0, 4);
  const lo = today.length ? Math.floor(Math.min(...today.map((s) => mins(s.start_time))) / 60) : 9;
  const hi = today.length ? Math.ceil(Math.max(...today.map((s) => mins(s.end_time))) / 60) : 17;
  const ROW = 72;

  return (
    <Card as="section" aria-labelledby="sch-h" className="glass-strong">
      <CardHeader
        id="sch-h"
        icon="calendar"
        title="Appointment schedule"
        subtitle="Seats booked of seats offered, per slot."
        aside={
          <>
            <label htmlFor="sch-clinic" className="sr-only">Clinic</label>
            <select id="sch-clinic" value={key} onChange={(e) => { setClinicKey(e.target.value); setDay(null); }} className={`${selectClass} min-h-9 max-w-48 py-0`}>
              {clinics.map((c) => <option key={c.id} value={c.clinic_key}>{c.name}</option>)}
            </select>
          </>
        }
      />
      <div className="px-5 pb-5">
        {res.error ? (
          <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load the schedule" />
        ) : !key ? (
          <p className="text-ink-muted">No clinic is registered, so there is no schedule.</p>
        ) : !res.data ? (
          <div className="skeleton h-64" role="status" aria-label="Loading the schedule" />
        ) : days.length === 0 ? (
          <p className="rounded-control border border-dashed border-line-strong px-4 py-6 text-ink-muted">This clinic has no capacity slots in the next 14 days.</p>
        ) : (
          <>
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-1.5">
                <button type="button" aria-label="Previous day" disabled={idx <= 0} onClick={() => setDay(days[idx - 1])} className="grid size-9 place-items-center rounded-full border border-line bg-surface disabled:opacity-40"><Icon name="chevron-right" className="size-4 rotate-180" /></button>
                <span className="tnum min-w-36 text-center font-semibold" aria-live="polite">{cur ? fmtDate(cur) : ""}</span>
                <button type="button" aria-label="Next day" disabled={idx >= days.length - 1} onClick={() => setDay(days[idx + 1])} className="grid size-9 place-items-center rounded-full border border-line bg-surface disabled:opacity-40"><Icon name="chevron-right" className="size-4" /></button>
              </div>
              <div role="group" aria-label="Schedule view" className="flex gap-1 rounded-full bg-brand-soft p-1">
                {(["day", "week"] as const).map((v) => (
                  <button key={v} type="button" aria-pressed={view === v} onClick={() => setView(v)} className={`min-h-8 rounded-full px-3.5 font-semibold ${view === v ? "bg-brand text-white" : "text-brand-ink"}`}>{humanize(v)}</button>
                ))}
              </div>
            </div>
            {view === "week" ? (
              <div role="region" aria-label="Week summary" tabIndex={0} className="overflow-x-auto">
                <table className="w-full min-w-[22rem] border-collapse text-left">
                  <caption className="sr-only">Seats by day for this clinic</caption>
                  <thead><tr className="border-b border-line text-ink-muted"><th scope="col" className="px-2 py-2 font-medium">Day</th><th scope="col" className="px-2 py-2 text-right font-medium">Slots</th><th scope="col" className="px-2 py-2 text-right font-medium">Booked</th><th scope="col" className="px-2 py-2 text-right font-medium">Offered</th><th scope="col" className="px-2 py-2 text-right font-medium">Use</th></tr></thead>
                  <tbody>
                    {days.map((d) => {
                      const ss = slots.filter((s) => dayOf(s.start_time) === d);
                      const b = ss.reduce((a, s) => a + s.reserved_capacity, 0);
                      const o = ss.reduce((a, s) => a + s.available_capacity, 0);
                      return <tr key={d} className="tnum border-b border-line last:border-0"><th scope="row" className="px-2 py-2.5 text-left font-medium">{fmtDate(d)}</th><td className="px-2 py-2.5 text-right">{ss.length}</td><td className="px-2 py-2.5 text-right">{fmtInt(b)}</td><td className="px-2 py-2.5 text-right">{fmtInt(o)}</td><td className="px-2 py-2.5 text-right"><Pill tone={tone(o ? b / o : null)}>{fmtShare(o ? b / o : null)}</Pill></td></tr>;
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div role="region" aria-label="Day schedule" tabIndex={0} className="overflow-x-auto">
                <div className="min-w-[30rem]">
                  <div className="ml-14 grid gap-1" style={{ gridTemplateColumns: `repeat(${services.length}, minmax(0, 1fr))` }}>
                    {services.map((s, i) => <p key={s} className="flex items-center gap-1.5 pb-1 text-[0.8125rem] font-medium"><span aria-hidden="true" className="size-2 rounded-full" style={{ background: SVC_COLORS[i] }} />{humanize(s)}</p>)}
                  </div>
                  <div className="relative flex" style={{ height: (hi - lo) * ROW }}>
                    <div className="w-14 shrink-0">
                      {Array.from({ length: hi - lo }, (_, i) => <p key={i} className="tnum text-xs text-ink-muted" style={{ height: ROW }}>{String(lo + i).padStart(2, "0")}:00</p>)}
                    </div>
                    <div className="relative grid flex-1 gap-1 rounded-control border border-line bg-canvas" style={{ gridTemplateColumns: `repeat(${services.length}, minmax(0, 1fr))` }}>
                      {services.map((svc, i) => (
                        <div key={svc} className="relative">
                          {today.filter((s) => s.service_key === svc).map((s) => {
                            const top = ((mins(s.start_time) - lo * 60) / 60) * ROW;
                            const h = Math.max(48, ((mins(s.end_time) - mins(s.start_time)) / 60) * ROW - 3);
                            return (
                              <div key={s.id} data-slot={s.id} className="absolute inset-x-1 overflow-hidden rounded-card border border-line bg-surface px-2.5 py-1 text-[0.8125rem] shadow-card" style={{ top, height: h }}>
                                <p className="flex items-center gap-1.5 truncate font-semibold"><span aria-hidden="true" className="size-2 shrink-0 rounded-full" style={{ background: SVC_COLORS[i] }} />{humanize(svc)}</p>
                                <p className="tnum text-ink-soft">{hhmm(s.start_time)}, {s.reserved_capacity} / {s.available_capacity} booked</p>
                              </div>
                            );
                          })}
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
                <p className="mt-2 text-xs text-ink-muted">Times are UTC. Slots are offered seats of the clinic, not individual appointments.</p>
              </div>
            )}
          </>
        )}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- bottom row
function Bottom({ selected, onSelect, rows, alerts }: { selected: string; onSelect: (d: string) => void; rows: { district_code: string; total_seats: number; reserved_seats: number; remaining_seats: number; utilization: number | null }[]; alerts: { key: string; title: string; message: string; status: string }[] }) {
  const over = rows.filter((r) => (r.utilization ?? 0) >= 0.95);
  const room = rows.filter((r) => r.utilization != null && r.utilization < 0.75);
  const movable = room.reduce((a, r) => a + r.remaining_seats, 0);
  return (
    <div className="grid gap-5 xl:grid-cols-3 *:min-w-0">
      <Card as="section" aria-labelledby="cbd-h" className="glass-strong">
        <CardHeader id="cbd-h" icon="layers" title="Capacity by district" aside={<DataLabel kind="live" />} />
        <ul aria-label="Seat use by district" className="space-y-1 px-3 pb-4">
          {rows.length === 0 && <li className="px-2 text-ink-muted">No capacity is registered.</li>}
          {rows.map((r) => (
            <li key={r.district_code}>
              <button type="button" aria-pressed={selected === r.district_code} onClick={() => onSelect(r.district_code)} className={`flex w-full items-center gap-3 rounded-control px-2.5 py-2 text-left ${selected === r.district_code ? "bg-brand-soft ring-1 ring-brand/40" : "hover:bg-canvas"}`}>
                <span className="w-16 shrink-0 text-ink-soft">{r.district_code}</span>
                <span className="h-2.5 min-w-8 flex-1 overflow-hidden rounded-full bg-brand-soft" aria-hidden="true"><GrowBar pct={(r.utilization ?? 0) * 100} /></span>
                <span className="tnum w-14 shrink-0 text-right font-semibold">{fmtShare(r.utilization)}</span>
                <span className="tnum hidden w-24 shrink-0 text-right text-xs text-ink-muted sm:block">{fmtInt(r.remaining_seats)} open</span>
              </button>
            </li>
          ))}
        </ul>
      </Card>
      <Card as="section" aria-labelledby="rb-h" className="glass-strong">
        <CardHeader id="rb-h" icon="refresh" title="Rebalance appointments" subtitle="Where seats are overloaded and where they are free." />
        <div className="space-y-3 px-5 pb-5">
          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-card bg-tint-rose px-4 py-3"><p className="tnum text-2xl font-bold">{over.length}</p><p className="text-[0.8125rem] text-ink-soft">{over.length === 1 ? "District" : "Districts"} above 95% use</p></div>
            <div className="rounded-card bg-tint-green px-4 py-3"><p className="tnum text-2xl font-bold">{room.length}</p><p className="text-[0.8125rem] text-ink-soft">{room.length === 1 ? "District" : "Districts"} below 75% use</p></div>
          </div>
          <p className="text-ink-soft">{over.length && room.length ? `${fmtInt(movable)} open seats in the lighter districts could take bookings from ${over.map((r) => r.district_code).join(", ")}.` : "No district needs rebalancing right now."} Moving bookings is done by a planning run, not from this screen.</p>
          <Link href="/planning" className="inline-flex items-center gap-1 text-brand-ink underline underline-offset-2">Open the planning studio <Icon name="arrow-right" className="size-4" /></Link>
        </div>
      </Card>
      <Card as="section" aria-labelledby="oa-h" className="glass-strong">
        <CardHeader id="oa-h" icon="alert" title={`Operational alerts (${alerts.length})`} />
        <ul className="space-y-2 px-5 pb-5">
          {alerts.length === 0 && <li className="text-ink-muted">No alert rule is firing.</li>}
          {alerts.map((a) => (
            <li key={a.key} className="flex items-start gap-2 rounded-card bg-tint-amber px-4 py-3">
              <Icon name="warning" className="mt-0.5 size-5 shrink-0 text-icon-amber" />
              <span><span className="block font-semibold">{a.title}</span><span className="text-[0.8125rem] text-ink-soft">{plain(a.message)}</span></span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

const clinicCols: Column<Clinic>[] = [
  { key: "n", header: "Clinic", cell: (c) => <><p className="font-semibold">{c.name}</p><p className="font-mono text-xs text-ink-muted">{c.clinic_key}</p></> },
  { key: "d", header: "District", cell: (c) => c.district_code },
  { key: "s", header: "Services", cell: (c) => (c.services.filter((s) => s.active).map((s) => s.service_name).join(", ") || "None") },
  { key: "o", header: "Status", cell: (c) => <StatusBadge status={c.operating_status} /> },
];

export function OperationsCapacity() {
  const [tab, setTab] = useState<Tab>("scheduling");
  const [district, setDistrict] = useState("");
  const clinics = useApi("ops-clinics", (s) => apiList<Clinic>("/planning/clinics", { page_size: 100 }, s));
  const cap = useCapacity();
  const eng = useEngagementOverview();
  const cc = useCommandCenter();
  const all = clinics.data?.data ?? [];
  const list = district ? all.filter((c) => c.district_code === district) : all;
  const by = cap.data?.data.by_district ?? [];
  const util = Object.fromEntries(by.map((r) => [r.district_code, r.utilization]));
  const c = cap.data?.data.capacity;
  const completed = eng.data?.data.appointments.by_status["completed"] ?? null;

  return (
    <>
      <PageHeader title="Operations and capacity" description="Manage appointments, resources and capacity across the care network." actions={<PillTabs tabs={TABS} active={tab} onChange={setTab} label="Operations views" panelId="ops-panel" />} />
      {district && (
        <p className="mb-4 flex flex-wrap items-center gap-2">
          Showing district <span className="rounded-full bg-brand-soft px-3 py-0.5 font-semibold text-brand-ink">{district}</span>
          <button type="button" onClick={() => setDistrict("")} className="text-brand-ink underline underline-offset-2">Show all districts</button>
        </p>
      )}
      <TabPanel id="ops-panel" label={TABS.find((t) => t.key === tab)!.label}>
        {tab === "scheduling" && (
          <div className="space-y-5">
            <div className="grid gap-5 xl:grid-cols-[1.15fr_1fr] *:min-w-0">
              <NetworkCard clinics={all} util={util} loading={!clinics.data && !clinics.error} selected={district} onSelect={(d) => setDistrict(d === district ? "" : d)} />
              {clinics.error ? <ErrorPanel error={clinics.error} onRetry={clinics.reload} title="Could not load clinics" /> : <Schedule key={district} clinics={list} />}
            </div>
            <section aria-label="Capacity figures" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
              <KpiTile icon="calendar-check" label="Appointments completed" value={completed == null ? null : fmtInt(completed)} basis="Simulated bookings that reached completed." loading={!eng.data && !eng.error} />
              <KpiTile icon="gauge" label="Capacity utilization" value={c?.utilization == null ? (c ? "Not available" : null) : fmtShare(c.utilization)} basis={c ? `${fmtInt(c.reserved_seats)} of ${fmtInt(c.total_seats)} seats booked.` : undefined} loading={!cap.data && !cap.error} />
              <KpiTile icon="users" label="Available capacity" value={c ? fmtInt(c.remaining_seats) : null} basis="Seats still open in the planning window." loading={!cap.data && !cap.error} />
              <KpiTile icon="clock" label="Median time to attend" value="Not available" basis="No source records the time from booking to attendance." />
            </section>
            <Bottom selected={district} onSelect={(d) => setDistrict(d === district ? "" : d)} rows={by} alerts={cc.data?.data.alerts ?? []} />
          </div>
        )}
        {tab === "resources" && (
          <DataTable caption="Clinics" columns={clinicCols} rows={clinics.data?.data ?? null} getRowId={(x) => x.id} loading={clinics.loading} error={clinics.error} onRetry={clinics.reload} emptyTitle="No clinics are registered" emptyHint="Clinics and their services appear here once they are registered." />
        )}
        {tab === "rebalancing" && <Bottom selected={district} onSelect={(d) => setDistrict(d === district ? "" : d)} rows={by} alerts={cc.data?.data.alerts ?? []} />}
        {tab === "network" && <NetworkCard clinics={all} util={util} loading={!clinics.data && !clinics.error} selected={district} onSelect={(d) => setDistrict(d === district ? "" : d)} />}
      </TabPanel>
    </>
  );
}
