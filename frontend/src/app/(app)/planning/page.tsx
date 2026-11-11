"use client";

import { useState } from "react";
import { api, apiList, isMissingEndpoint } from "@/lib/api";
import { fmtDateTime, fmtInt, fmtPct, humanize } from "@/lib/format";
import type { CapacityOverview, Clinic, PlanningRun } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { SimulatedBadge } from "@/components/badges";
import { DataTable, type Column } from "@/components/data-table";
import { StatTile } from "@/components/stat-tile";
import { StatusBadge } from "@/components/status-badge";
import { ErrorPanel, PageHeader, Section, Unavailable, btnSecondary } from "@/components/ui";
import { RunPanel } from "./run-panel";

type DistrictRow = CapacityOverview["by_district"][number];
const districtCols: Column<DistrictRow>[] = [
  { key: "d", header: "District", cell: (r) => r.district_code },
  { key: "t", header: "Total seats", align: "right", cell: (r) => fmtInt(r.total_seats) },
  { key: "r", header: "Reserved", align: "right", cell: (r) => fmtInt(r.reserved_seats) },
  { key: "p", header: "Proposed", align: "right", cell: (r) => fmtInt(r.planned_seats) },
  { key: "rem", header: "Remaining", align: "right", cell: (r) => fmtInt(r.remaining_seats) },
  { key: "u", header: "Utilisation", align: "right", cell: (r) => fmtPct(r.utilization) },
];

const clinicCols: Column<Clinic>[] = [
  {
    key: "name",
    header: "Clinic",
    cell: (c) => (
      <>
        <p className="font-medium">{c.name}</p>
        <p className="font-mono text-xs text-ink-muted">{c.clinic_key}</p>
      </>
    ),
  },
  { key: "district", header: "District", cell: (c) => c.district_code },
  { key: "services", header: "Services", cell: (c) => (c.services.length ? c.services.filter((s) => s.active).map((s) => s.service_name).join(", ") : "None") },
  { key: "status", header: "Status", cell: (c) => <StatusBadge status={c.operating_status === "open" ? "healthy" : "unavailable"} /> },
  { key: "label", header: "Data", cell: (c) => <SimulatedBadge>{humanize(c.synthetic_status)}</SimulatedBadge> },
];

function Capacity() {
  const res = useApi("capacity", (signal) => api<CapacityOverview>("/analytics/capacity", { signal }));
  if (res.error) {
    return isMissingEndpoint(res.error) ? <Unavailable endpoint="GET /api/v1/analytics/capacity" /> : <ErrorPanel error={res.error} onRetry={res.reload} />;
  }
  const c = res.data?.data;
  const cap = c?.capacity;
  const n = (v: number | undefined) => (c && v != null ? fmtInt(v) : null);
  const lr = c?.latest_run;
  return (
    <>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile loading={res.loading} label="Total seats" value={n(cap?.total_seats)} basis="open capacity slots" badge={<SimulatedBadge />} />
        <StatTile loading={res.loading} label="Reserved" value={n(cap?.reserved_seats)} basis="confirmed allocations only" />
        <StatTile loading={res.loading} label="Remaining" value={n(cap?.remaining_seats)} basis="total minus reserved" />
        <StatTile loading={res.loading} label="Utilisation" value={cap && cap.utilization != null ? fmtPct(cap.utilization) : null} basis="reserved of total seats" />
      </div>
      {c && (
        <p className="mt-2 max-w-[75ch] text-ink-muted">
          {c.window.start || c.window.end ? `Window ${c.window.start?.slice(0, 10) ?? "open"} to ${c.window.end?.slice(0, 10) ?? "open"} (${humanize(c.window.source)}). ` : ""}
          {cap?.note}
          {lr ? ` Latest completed run: ${fmtInt(lr.allocated)} of ${fmtInt(lr.cases_total)} cases allocated (${fmtPct(lr.coverage)}), ${fmtInt(lr.unallocated)} unallocated.` : " No planning run has completed yet."}
        </p>
      )}
      {c && c.by_district.length > 0 && (
        <div className="mt-3">
          <DataTable caption="Capacity by district" columns={districtCols} rows={c.by_district} getRowId={(r) => r.district_code} loading={false} error={null} />
        </div>
      )}
    </>
  );
}

export default function PlanningPage() {
  const [cpage, setCpage] = useState(1);
  const clinics = useApi(`clinics:${cpage}`, (signal) => apiList<Clinic>("/planning/clinics", { page: cpage, page_size: 25 }, signal));
  const runs = useApi("runs", (signal) => apiList<PlanningRun>("/planning/runs", { page_size: 25 }, signal));
  const [runId, setRunId] = useState<string | null>(null);
  const cmeta = clinics.data?.meta;

  const runCols: Column<PlanningRun>[] = [
    { key: "id", header: "Run", cell: (r) => <span className="font-mono text-xs">{r.id.slice(0, 8)}</span> },
    { key: "status", header: "Status", cell: (r) => <StatusBadge status={r.status} /> },
    {
      key: "period",
      header: "Planning period",
      className: "whitespace-nowrap tnum",
      cell: (r) => `${r.planning_period_start.slice(0, 10)} to ${r.planning_period_end.slice(0, 10)}`,
    },
    { key: "cases", header: "Cases", align: "right", cell: (r) => fmtInt(r.metrics.cases_total) },
    { key: "alloc", header: "Allocated", align: "right", cell: (r) => fmtInt(r.metrics.allocated) },
    { key: "created", header: "Created", className: "whitespace-nowrap tnum", cell: (r) => fmtDateTime(r.created_at) },
    {
      key: "open",
      header: "Details",
      cell: (r) => (
        <button
          type="button"
          className={btnSecondary}
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
        title="Planning"
        description="Clinic capacity and how cases are allocated to it. Allocations are proposals until confirmed, confirming reserves a seat only, and every booking here is simulated."
        actions={
          <button
            type="button"
            className={btnSecondary}
            onClick={() => {
              clinics.reload();
              runs.reload();
            }}
          >
            Refresh
          </button>
        }
      />
      <Section id="capacity" title="Capacity">
        <Capacity />
      </Section>
      <Section id="clinics" title="Clinics">
        <DataTable
          caption="Clinics"
          columns={clinicCols}
          rows={clinics.data?.data ?? null}
          getRowId={(c) => c.id}
          loading={clinics.loading}
          error={clinics.error}
          onRetry={clinics.reload}
          unavailable={{
            endpoint: "GET /api/v1/planning/clinics",
            willShow: ["Clinics with their services.", "Planning runs, proposed allocations and why cases stayed unallocated."],
          }}
          emptyTitle="No clinics are registered"
          emptyHint="Register clinics and capacity slots before running the planner."
          page={cmeta?.page ?? cpage}
          pageSize={cmeta?.page_size ?? 25}
          total={cmeta?.total}
          totalPages={cmeta?.total_pages}
          onPageChange={setCpage}
        />
      </Section>
      <Section id="runs" title="Planning runs">
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
      </Section>
    </>
  );
}
