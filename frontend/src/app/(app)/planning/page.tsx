"use client";

import { useState } from "react";
import { api, apiList, isMissingEndpoint } from "@/lib/api";
import { fmtDate, fmtDateTime, fmtInt, fmtPct } from "@/lib/format";
import type { CapacityOverview, Clinic, PlanningRun, RunMetrics } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { DataTable, type Column } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { Card, CardHeader, ColumnPairs, DataLabel, ErrorPanel, KpiTile, PageHeader, Pill, Unavailable, btnSecondary, Icon } from "@/components/ui";
import { PillTabs, PlanningStudio, TabPanel, type PillTab } from "@/components/features";
import { RunPanel } from "./run-panel";

type Tab = "plan" | "capacity" | "allocate" | "track";
const TABS: PillTab<Tab>[] = [
  { key: "plan", label: "Plan", icon: "target" },
  { key: "capacity", label: "Capacity", icon: "building" },
  { key: "allocate", label: "Allocate", icon: "calendar-check" },
  { key: "track", label: "Track", icon: "activity" },
];

type DistrictRow = CapacityOverview["by_district"][number];
const districtCols: Column<DistrictRow>[] = [
  {
    key: "d",
    header: "District",
    cell: (r) => (
      <span className="flex items-center gap-2 font-medium">
        <Icon name="pin" className="size-4 text-brand-ink" />
        {r.district_code}
      </span>
    ),
  },
  { key: "t", header: "Total seats", align: "right", cell: (r) => fmtInt(r.total_seats) },
  { key: "p", header: "Proposed", align: "right", cell: (r) => fmtInt(r.planned_seats) },
  { key: "r", header: "Reserved", align: "right", cell: (r) => fmtInt(r.reserved_seats) },
  { key: "rem", header: "Remaining", align: "right", cell: (r) => fmtInt(r.remaining_seats) },
  {
    key: "u",
    header: "Use",
    align: "right",
    cell: (r) => (r.utilization == null ? "None" : <Pill tone={r.utilization >= 0.9 ? "warn" : "ok"}>{fmtPct(r.utilization)}</Pill>),
  },
];

const clinicCols: Column<Clinic>[] = [
  {
    key: "name",
    header: "Clinic",
    cell: (c) => (
      <>
        <p className="font-semibold">{c.name}</p>
        <p className="font-mono text-xs text-ink-muted">{c.clinic_key}</p>
      </>
    ),
  },
  { key: "district", header: "District", cell: (c) => c.district_code },
  { key: "services", header: "Services", cell: (c) => (c.services.length ? c.services.filter((s) => s.active).map((s) => s.service_name).join(", ") : "None") },
  { key: "status", header: "Status", cell: (c) => <StatusBadge status={c.operating_status} /> },
];

function Capacity() {
  const res = useApi("capacity", (signal) => api<CapacityOverview>("/analytics/capacity", { signal }));
  if (res.error) {
    return isMissingEndpoint(res.error) ? <Unavailable endpoint="GET /api/v1/analytics/capacity" /> : <ErrorPanel error={res.error} onRetry={res.reload} />;
  }
  const c = res.data?.data;
  const cap = c?.capacity;
  const lr = c?.latest_run;
  const n = (v: number | undefined) => (c && v != null ? fmtInt(v) : null);
  return (
    <>
      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <KpiTile loading={res.loading} icon="building" tint="teal" label="Total seats" value={n(cap?.total_seats)} basis="Open capacity slots" badge={<DataLabel kind="live" />} />
        <KpiTile loading={res.loading} icon="calendar-check" tint="blue" label="Reserved" value={n(cap?.reserved_seats)} basis="Confirmed allocations only" />
        <KpiTile loading={res.loading} icon="gauge" tint="green" label="Remaining" value={n(cap?.remaining_seats)} basis="Total minus reserved" />
        <KpiTile loading={res.loading} icon="activity" tint="amber" label="Seat use" value={cap && cap.utilization != null ? fmtPct(cap.utilization) : null} basis="Reserved of total seats" />
        <KpiTile
          loading={res.loading}
          icon="target"
          tint="violet"
          label="Latest plan coverage"
          value={lr ? fmtPct(lr.coverage) : null}
          basis={lr ? `${fmtInt(lr.allocated)} of ${fmtInt(lr.cases_total)} cases allocated` : "No run has completed yet"}
        />
      </div>
      {c && (
        <div className="mb-6 grid gap-4 xl:grid-cols-[1.2fr_1fr] [&>*]:min-w-0">
          <Card as="section" aria-labelledby="pl-chart">
            <CardHeader id="pl-chart" title="Capacity and planned allocation" subtitle="Seats and proposed allocations by district" aside={<DataLabel kind="live" />} />
            <div className="px-5 pb-5">
              {c.by_district.length ? (
                <ColumnPairs
                  groups={c.by_district.map((d) => ({ label: d.district_code, a: d.planned_seats, b: d.total_seats }))}
                  aLabel="Planned allocation"
                  bLabel="Available capacity"
                  ariaLabel="Planned allocation and available capacity by district"
                  tableCaption="Planned allocation and available capacity by district"
                />
              ) : (
                <p className="py-10 text-center text-ink-muted">No capacity has been registered yet.</p>
              )}
            </div>
          </Card>
          <Card as="section" aria-labelledby="pl-note">
            <CardHeader id="pl-note" icon="info" title="How to read this" />
            <div className="space-y-3 px-5 pb-5 text-ink-soft">
              <p>{c.window.start || c.window.end ? `Window ${c.window.start ? fmtDate(c.window.start) : "open"} to ${c.window.end ? fmtDate(c.window.end) : "open"}.` : "All registered capacity."}</p>
              <p>Proposed seats are plans, not bookings. Reserved seats are confirmed allocations. A confirmed allocation holds the slot for the case.</p>
              {lr && (
                <p>
                  Latest completed run: {fmtInt(lr.allocated)} of {fmtInt(lr.cases_total)} cases allocated, {fmtInt(lr.unallocated)} without a slot.
                </p>
              )}
            </div>
          </Card>
        </div>
      )}
      {c && c.by_district.length > 0 && (
        <div className="mb-8">
          <h2 className="mb-3 text-lg font-semibold tracking-tight">District capacity</h2>
          <DataTable caption="Capacity by district" columns={districtCols} rows={c.by_district} getRowId={(r) => r.district_code} loading={false} error={null} />
        </div>
      )}
    </>
  );
}

export default function PlanningPage() {
  const [tab, setTab] = useState<Tab>("plan");
  const [cpage, setCpage] = useState(1);
  const clinics = useApi(`clinics:${cpage}`, (signal) => apiList<Clinic>("/planning/clinics", { page: cpage, page_size: 25 }, signal));
  const runs = useApi("runs", (signal) => apiList<PlanningRun>("/planning/runs", { page_size: 25 }, signal));
  const [runId, setRunId] = useState<string | null>(null);
  const cmeta = clinics.data?.meta;

  const runCols: Column<PlanningRun>[] = [
    { key: "id", header: "Run", cell: (r) => <span className="font-mono text-xs">{r.id.slice(0, 8)}</span> },
    { key: "status", header: "Status", cell: (r) => <StatusBadge status={r.status} /> },
    { key: "period", header: "Planning period", className: "whitespace-nowrap tnum", cell: (r) => `${fmtDate(r.planning_period_start)} to ${fmtDate(r.planning_period_end)}` },
    { key: "cases", header: "Cases", align: "right", cell: (r) => fmtInt((r.metrics as RunMetrics).cases_total) },
    { key: "alloc", header: "Allocated", align: "right", cell: (r) => fmtInt((r.metrics as RunMetrics).allocated) },
    { key: "created", header: "Created", className: "whitespace-nowrap tnum", cell: (r) => fmtDateTime(r.created_at) },
    {
      key: "open",
      header: "Details",
      cell: (r) => (
        <button
          type="button"
          className={`${btnSecondary} min-h-9 px-3`}
          aria-pressed={runId === r.id}
          aria-controls={runId === r.id ? "run-panel" : undefined}
          onClick={() => setRunId(runId === r.id ? null : r.id)}
        >
          {runId === r.id ? "Hide run" : "Open run"}
        </button>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Care planning studio"
        description="Create capacity aware preventive care plans for your population. Allocations are proposals until confirmed, and confirming reserves a seat."
        actions={<PillTabs tabs={TABS} active={tab} onChange={setTab} label="Planning views" panelId="planning-panel" />}
      />
      <TabPanel id="planning-panel" label={TABS.find((x) => x.key === tab)!.label}>
        {tab === "plan" && <PlanningStudio onGenerated={() => runs.reload()} onOpenAllocations={() => setTab("allocate")} />}
        {tab === "capacity" && (
          <>
            <Capacity />
            <section aria-labelledby="clinics" className="mb-8">
        <h2 id="clinics" className="mb-3 text-lg font-semibold tracking-tight">
          Clinics
        </h2>
        <DataTable
          caption="Clinics"
          columns={clinicCols}
          rows={clinics.data?.data ?? null}
          getRowId={(c) => c.id}
          loading={clinics.loading}
          error={clinics.error}
          onRetry={clinics.reload}
          unavailable={{ endpoint: "GET /api/v1/planning/clinics", willShow: ["Clinics with their services.", "Planning runs, proposed allocations and why cases stayed unallocated."] }}
          emptyTitle="No clinics are registered"
          emptyHint="Register clinics and capacity slots before running the planner."
          page={cmeta?.page ?? cpage}
          pageSize={cmeta?.page_size ?? 25}
          total={cmeta?.total}
          totalPages={cmeta?.total_pages}
          onPageChange={setCpage}
        />
            </section>
          </>
        )}
        {(tab === "track" || tab === "allocate") && (
          <section aria-labelledby="runs" className="mb-8">
            <h2 id="runs" className="mb-3 text-lg font-semibold tracking-tight">
              {tab === "allocate" ? "Choose a run to see its allocations" : "Planning runs"}
            </h2>
        <DataTable
          caption="Planning runs"
          columns={runCols}
          rows={runs.data?.data ?? null}
          getRowId={(r) => r.id}
          loading={runs.loading}
          error={runs.error}
          onRetry={runs.reload}
          unavailable={{ endpoint: "GET /api/v1/planning/runs" }}
          emptyTitle="No planning runs yet"
          emptyHint="A run allocates approved cases to the capacity available in a planning period."
        />
            {runId && <RunPanel key={runId} runId={runId} />}
          </section>
        )}
      </TabPanel>
    </>
  );
}
