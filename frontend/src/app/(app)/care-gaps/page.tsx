"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { api, apiList } from "@/lib/api";
import { fmtDate, fmtInt, fmtPct, humanize } from "@/lib/format";
import type { CareGap, CommandCenter } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { DataTable, type Column } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { KpiTile, PageHeader, PriorityPill, btnGhost, btnPrimary, btnSecondary, inputClass, selectClass, type IconName, Icon } from "@/components/ui";

const PAGE_SIZE = 25;
// backend/anticipa/caregaps/models.py CASE_STATUSES and PRIORITIES
const STATUSES = [
  "identified", "pending_review", "approved_for_planning", "awaiting_capacity", "planned", "outreach_pending",
  "outreach_completed", "appointment_scheduled", "awaiting_completion_evidence", "escalated", "completed",
  "closed_not_completed", "cancelled",
] as const;
const PRIORITIES = [
  ["high", "High"],
  ["elevated", "Medium"],
  ["routine", "Low"],
] as const;
const EMPTY = { status: "", priority: "", district: "", care_activity: "" };

/** A recognisable icon per care activity. Falls back to a generic target. */
function activityIcon(activity: string): IconName {
  const a = activity.toLowerCase();
  if (/breast|cervical|colorectal|cancer|mammo/.test(a)) return "ribbon";
  if (/diabet|hba1c|glucose/.test(a)) return "droplet";
  if (/immun|vaccin/.test(a)) return "syringe";
  if (/hypertens|blood_pressure|cardio/.test(a)) return "heart";
  return "target";
}

const columns: Column<CareGap>[] = [
  {
    key: "resident",
    header: "Resident",
    cell: (g) => (
      <Link href={`/residents/${g.resident_id}`} className="font-mono text-[0.8125rem] font-semibold text-brand-ink underline underline-offset-2">
        {g.resident_id.slice(0, 8)}
      </Link>
    ),
  },
  {
    key: "activity",
    header: "Care gap",
    cell: (g) => (
      <span className="flex items-center gap-2.5">
        <span className="grid size-8 shrink-0 place-items-center icon-tile">
          <Icon name={activityIcon(g.care_activity)} className="size-4" />
        </span>
        <span className="font-medium">{humanize(g.care_activity)}</span>
      </span>
    ),
  },
  { key: "priority", header: "Priority", cell: (g) => <PriorityPill priority={g.priority} /> },
  { key: "status", header: "Status", cell: (g) => <StatusBadge status={g.status} /> },
  { key: "outcome", header: "Rule outcome", cell: (g) => <StatusBadge status={g.source_outcome} /> },
  { key: "district", header: "District", cell: (g) => g.district_code ?? "None" },
  { key: "opened", header: "Opened", className: "whitespace-nowrap tnum", cell: (g) => fmtDate(g.opened_at) },
  {
    key: "open",
    header: "Case",
    cell: (g) => (
      <Link href={`/care-gaps/${g.id}`} className={`${btnSecondary} min-h-9 px-3`}>
        Open case
      </Link>
    ),
  },
];

export default function CareGapsPage() {
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState(EMPTY);
  // Links from the Programs page open this list already filtered (status, priority, care_activity in the URL).
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const f = { status: q.get("status") ?? "", priority: q.get("priority") ?? "", district: "", care_activity: q.get("care_activity") ?? "" };
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (Object.values(f).some(Boolean)) setFilters(f);
  }, []);
  const res = useApi(`care-gaps:${page}:${JSON.stringify(filters)}`, (signal) => apiList<CareGap>("/care-gaps", { page, page_size: PAGE_SIZE, ...filters }, signal));
  const cc = useApi("command-center:care-gaps", (signal) => api<CommandCenter>("/analytics/command-center", { signal }));
  const meta = res.data?.meta;
  const filtered = Object.values(filters).some(Boolean);
  const k = (key: string) => cc.data?.data.kpis.find((x) => x.key === key);
  const highOpen = cc.data?.data.gaps_by_priority.items.find((p) => p.priority === "high")?.open;
  const ccLoading = cc.loading && !cc.data;
  const ccFailed = !!cc.error;

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const v = (n: string) => String(f.get(n) ?? "").trim();
    setFilters({ status: v("status"), priority: v("priority"), district: v("district").toUpperCase(), care_activity: filters.care_activity });
    setPage(1);
  }

  return (
    <>
      <PageHeader
        title="Care gaps"
        description="Find, prioritise and close preventive care gaps with verified evidence. Each case names the guideline and rule version that raised it."
        actions={
          <button type="button" className={btnSecondary} onClick={() => { res.reload(); cc.reload(); }} disabled={res.loading}>
            <Icon name="refresh" className="size-4" />
            Refresh
          </button>
        }
      />

      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <KpiTile loading={ccLoading} icon="users" tint="teal" label="Residents with care gaps" value={ccFailed ? null : k("residents_with_open_gaps") ? fmtInt(k("residents_with_open_gaps")?.value) : null} basis="Residents with at least one open case" />
        <KpiTile loading={ccLoading} icon="target" tint="rose" label="High priority open" value={ccFailed || highOpen == null ? null : fmtInt(highOpen)} basis="Open cases with high priority" />
        <KpiTile loading={ccLoading} icon="check-circle" tint="green" label="Closed with verified evidence" value={ccFailed ? null : k("gaps_closed") ? fmtInt(k("gaps_closed")?.value) : null} basis="Cases completed" />
        <KpiTile
          loading={ccLoading}
          icon="chart"
          tint="blue"
          label="Closure rate"
          value={ccFailed ? null : k("closure_rate") ? fmtPct(k("closure_rate")?.value) : null}
          basis={k("closure_rate")?.numerator != null ? `${fmtInt(k("closure_rate")?.numerator)} of ${fmtInt(k("closure_rate")?.denominator)} cases` : "Completed cases divided by all cases"}
        />
      </div>

      <form key={JSON.stringify(filters)} onSubmit={onSubmit} aria-label="Filter care gaps" className="card mb-6 flex flex-wrap items-end gap-4 p-4">
        <div className="w-full sm:w-52">
          <label htmlFor="gap-status" className="mb-1.5 block font-medium">
            Status
          </label>
          <select id="gap-status" name="status" defaultValue={filters.status} className={selectClass}>
            <option value="">All statuses</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {humanize(s)}
              </option>
            ))}
          </select>
        </div>
        <div className="w-full sm:w-44">
          <label htmlFor="gap-priority" className="mb-1.5 block font-medium">
            Priority
          </label>
          <select id="gap-priority" name="priority" defaultValue={filters.priority} className={selectClass}>
            <option value="">All priorities</option>
            {PRIORITIES.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </div>
        <div className="w-full sm:w-36">
          <label htmlFor="gap-district" className="mb-1.5 block font-medium">
            District
          </label>
          <input id="gap-district" name="district" defaultValue={filters.district} className={inputClass} placeholder="For example D03" />
        </div>
        <div className="ml-auto flex gap-2">
          <button
            type="reset"
            className={btnGhost}
            onClick={() => {
              setFilters(EMPTY);
              setPage(1);
            }}
          >
            Clear filters
          </button>
          <button type="submit" className={btnPrimary}>
            <Icon name="filter" className="size-4" />
            Apply filters
          </button>
        </div>
      </form>
      {filters.care_activity && (
        <p className="mb-4 flex flex-wrap items-center gap-2">
          Showing program <span className="rounded-full bg-brand-soft px-3 py-0.5 font-semibold text-brand-ink">{humanize(filters.care_activity)}</span>
          <button type="button" className={btnGhost} onClick={() => { setFilters({ ...filters, care_activity: "" }); setPage(1); }}>
            Show all programs
          </button>
        </p>
      )}

      <DataTable
        caption="Care gap cases"
        columns={columns}
        rows={res.data?.data ?? null}
        getRowId={(g) => g.id}
        loading={res.loading}
        error={res.error}
        onRetry={res.reload}
        unavailable={{
          endpoint: "GET /api/v1/care-gaps",
          willShow: ["Cases by status and priority, each linked to the guideline and rule version that raised it.", "The supporting evidence for each case and its full status history."],
        }}
        emptyTitle={filtered ? "No cases match these filters" : "No care gap cases yet"}
        emptyHint={filtered ? "Clear a filter to see more cases." : "Cases are created when approved rules are evaluated against loaded residents."}
        page={meta?.page ?? page}
        pageSize={meta?.page_size ?? PAGE_SIZE}
        total={meta?.total}
        totalPages={meta?.total_pages}
        onPageChange={setPage}
      />
    </>
  );
}
