"use client";

import Link from "next/link";
import { useState } from "react";
import { api, apiList, apiPost } from "@/lib/api";
import { fmtDateTime, fmtInt, fmtPct, humanize } from "@/lib/format";
import { PERM } from "@/lib/nav";
import type { Allocation, ConfirmResult, GroupMetrics, PlanningRun, RunMetrics, Unallocated } from "@/lib/types";
import { useAction } from "@/lib/use-action";
import { useApi } from "@/lib/use-api";
import { SimulatedBadge } from "@/components/badges";
import { DataTable, type Column } from "@/components/data-table";
import { GatedButton } from "@/components/gated-button";
import { StatTile } from "@/components/stat-tile";
import { StatusBadge } from "@/components/status-badge";
import { AsyncBlock, ErrorPanel, Notice, Section, btnPrimary } from "@/components/ui";
import { displayText } from "@/lib/display";

type Group = { key: string } & GroupMetrics;
const groupCols = (label: string): Column<Group>[] => [
  { key: "k", header: label, cell: (g) => humanize(g.key) },
  { key: "d", header: "Cases needing a slot", align: "right", cell: (g) => fmtInt(g.demand) },
  { key: "a", header: "Allocated", align: "right", cell: (g) => fmtInt(g.allocated) },
  { key: "c", header: "Coverage", align: "right", cell: (g) => fmtPct(g.coverage) },
];
const groups = (m: Record<string, GroupMetrics> | undefined): Group[] => Object.entries(m ?? {}).map(([key, v]) => ({ key, ...v }));

export function RunPanel({ runId }: { runId: string }) {
  const run = useApi(`run:${runId}`, (signal) => api<PlanningRun>(`/planning/runs/${runId}`, { signal }));
  const [apage, setApage] = useState(1);
  const [upage, setUpage] = useState(1);
  const allocs = useApi(`run-allocs:${runId}:${apage}`, (signal) =>
    apiList<Allocation>(`/planning/runs/${runId}/allocations`, { page: apage, page_size: 25 }, signal),
  );
  const unalloc = useApi(`run-unalloc:${runId}:${upage}`, (signal) =>
    apiList<Unallocated>(`/planning/runs/${runId}/unallocated`, { page: upage, page_size: 25 }, signal),
  );
  const confirm = useAction();
  const [confirmed, setConfirmed] = useState<ConfirmResult | null>(null);
  const r = run.data?.data;
  const m = r?.metrics as RunMetrics | undefined;

  const allocCols: Column<Allocation>[] = [
    {
      key: "case",
      header: "Case",
      cell: (a) => (
        <Link href={`/care-gaps/${a.care_gap_case_id}`} className="font-mono text-xs text-brand-ink underline underline-offset-2">
          {a.care_gap_case_id.slice(0, 8)}
        </Link>
      ),
    },
    { key: "clinic", header: "Clinic and service", cell: (a) => `${a.clinic_key}, ${humanize(a.service_key)}` },
    { key: "slot", header: "Proposed time", className: "whitespace-nowrap tnum", cell: (a) => fmtDateTime(a.proposed_start_time) },
    { key: "pri", header: "Priority", cell: (a) => humanize(a.priority) },
    {
      key: "status",
      header: "Status",
      cell: (a) => (
        <span className="inline-flex flex-wrap items-center gap-1.5">
          <StatusBadge status={a.allocation_status} />
          {a.allocation_status === "proposed" && <span className="text-xs text-ink-muted">A proposal, not an appointment</span>}
          <SimulatedBadge>Simulated booking</SimulatedBadge>
        </span>
      ),
    },
    {
      key: "act",
      header: "Action",
      cell: (a) =>
        a.allocation_status === "proposed" ? (
          <GatedButton
            permission={PERM.capacityManage}
            className={btnPrimary}
            disabled={confirm.pending}
            onClick={async () => {
              setConfirmed(null);
              let result: ConfirmResult | null = null;
              if (
                await confirm.run(async () => {
                  result = (await apiPost<ConfirmResult>(`/planning/allocations/${a.id}/confirm`)).data;
                })
              ) {
                setConfirmed(result);
                allocs.reload();
              }
            }}
          >
            Confirm allocation
          </GatedButton>
        ) : (
          <span className="text-ink-muted">None</span>
        ),
    },
  ];

  const unallocCols: Column<Unallocated>[] = [
    {
      key: "case",
      header: "Case",
      cell: (u) => (
        <Link href={`/care-gaps/${u.care_gap_case_id}`} className="font-mono text-xs text-brand-ink underline underline-offset-2">
          {u.care_gap_case_id.slice(0, 8)}
        </Link>
      ),
    },
    { key: "reason", header: "Reason", cell: (u) => <span className="font-medium">{humanize(u.reason_code)}</span> },
    { key: "why", header: "Explanation", cell: (u) => displayText(u.explanation) },
    { key: "district", header: "District", cell: (u) => u.district_code ?? "None" },
    { key: "lang", header: "Language", cell: (u) => u.language ?? "None" },
  ];

  const ameta = allocs.data?.meta;
  const umeta = unalloc.data?.meta;
  return (
    <div id="run-panel" className="card mt-4 space-y-5 p-5">
      <h3 className="text-base font-semibold">
        Run <span className="font-mono text-sm">{runId.slice(0, 8)}</span>
      </h3>
      <AsyncBlock
        loading={run.loading && !r}
        error={run.error}
        onRetry={run.reload}
        endpoint="GET /api/v1/planning/runs/{id}"
        empty={!r}
        emptyTitle="Run not found"
      >
        {r && (
          <>
            {r.error_summary && (
              <div role="alert" className="rounded-md border border-danger bg-danger-bg px-4 py-3 text-danger">
                <p className="font-semibold">This run reported an error</p>
                <p className="mt-0.5 whitespace-pre-wrap">{r.error_summary}</p>
              </div>
            )}
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatTile label="Status" value={<StatusBadge status={r.status} />} basis={`algorithm ${r.algorithm_version}, seed ${r.seed}`} />
              <StatTile label="Cases considered" value={m?.cases_total != null ? fmtInt(m.cases_total) : null} basis="approved cases in the planning period" />
              <StatTile label="Allocated" value={m?.allocated != null ? fmtInt(m.allocated) : null} basis={`coverage ${fmtPct(m?.coverage)} of cases considered`} />
              <StatTile label="Unallocated" value={m?.unallocated != null ? fmtInt(m.unallocated) : null} basis="no feasible slot, see reasons below" />
            </div>
          </>
        )}
      </AsyncBlock>

      {r && (groups(m?.by_district).length > 0 || groups(m?.by_language).length > 0) && (
        <Section id="equity" title="Equity: who was reached">
          <p className="mb-2 max-w-[65ch] text-ink-muted">
            Coverage is allocated cases divided by cases needing a slot in each group, so gaps between groups are visible.
          </p>
          <div className="grid gap-4 lg:grid-cols-2 [&>*]:min-w-0">
            <DataTable caption="Coverage by district" columns={groupCols("District")} rows={groups(m?.by_district)} getRowId={(g) => g.key} loading={false} error={null} />
            <DataTable caption="Coverage by language" columns={groupCols("Language")} rows={groups(m?.by_language)} getRowId={(g) => g.key} loading={false} error={null} />
          </div>
        </Section>
      )}

      <Section id="unalloc" title="Why cases were not allocated">
        <DataTable
          caption="Unallocated cases"
          columns={unallocCols}
          rows={unalloc.data?.data ?? null}
          getRowId={(u) => u.id}
          loading={unalloc.loading}
          error={unalloc.error}
          onRetry={unalloc.reload}
          unavailable={{ endpoint: "GET /api/v1/planning/runs/{id}/unallocated" }}
          emptyTitle="Every considered case was allocated"
          page={umeta?.page ?? upage}
          pageSize={umeta?.page_size ?? 25}
          total={umeta?.total}
          totalPages={umeta?.total_pages}
          onPageChange={setUpage}
        />
      </Section>

      <Section id="allocs" title="Proposed allocations">
        {confirmed && (
          <Notice tone="ok">
            {confirmed.newly_confirmed ? "Allocation confirmed." : "This allocation was already confirmed."} A seat is reserved.{" "}
            {confirmed.appointment_created ? "A simulated appointment now holds the slot for the case." : "No new appointment was created."}
          </Notice>
        )}
        {confirm.error && (
          <div className="mb-2">
            <ErrorPanel error={confirm.error} title="The allocation was not confirmed" />
          </div>
        )}
        <DataTable
          caption="Allocations"
          columns={allocCols}
          rows={allocs.data?.data ?? null}
          getRowId={(a) => a.id}
          loading={allocs.loading}
          error={allocs.error}
          onRetry={allocs.reload}
          unavailable={{ endpoint: "GET /api/v1/planning/runs/{id}/allocations" }}
          emptyTitle="This run produced no allocations"
          page={ameta?.page ?? apage}
          pageSize={ameta?.page_size ?? 25}
          total={ameta?.total}
          totalPages={ameta?.total_pages}
          onPageChange={setApage}
        />
      </Section>
    </div>
  );
}
