"use client";

import Link from "next/link";
import { useState } from "react";
import { apiList } from "@/lib/api";
import { fmtDateTime, humanize } from "@/lib/format";
import type { CareGap } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { SyntheticBadge } from "@/components/badges";
import { DataTable, type Column } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { PageHeader, btnSecondary } from "@/components/ui";

const PAGE_SIZE = 25;
// backend/anticipa/caregaps/models.py CASE_STATUSES and PRIORITIES
const STATUSES = [
  "identified", "pending_review", "approved_for_planning", "awaiting_capacity", "planned", "outreach_pending",
  "outreach_completed", "appointment_scheduled", "awaiting_completion_evidence", "escalated", "completed",
  "closed_not_completed", "cancelled",
] as const;
const PRIORITIES = ["high", "elevated", "routine"] as const;

const columns: Column<CareGap>[] = [
  {
    key: "activity",
    header: "Care activity",
    cell: (g) => (
      <Link href={`/care-gaps/${g.id}`} className="font-medium text-link underline underline-offset-2">
        {humanize(g.care_activity)}
      </Link>
    ),
  },
  {
    key: "resident",
    header: "Resident",
    cell: (g) => (
      <Link href={`/residents/${g.resident_id}`} className="font-mono text-xs text-link underline underline-offset-2">
        {g.resident_id.slice(0, 8)}
      </Link>
    ),
  },
  { key: "status", header: "Status", cell: (g) => <StatusBadge status={g.status} /> },
  { key: "priority", header: "Priority", cell: (g) => humanize(g.priority) },
  { key: "outcome", header: "Rule outcome", cell: (g) => <StatusBadge status={g.source_outcome} /> },
  { key: "district", header: "District", cell: (g) => g.district_code ?? "—" },
  { key: "opened", header: "Opened", className: "whitespace-nowrap tnum", cell: (g) => fmtDateTime(g.opened_at) },
  { key: "label", header: "Data", cell: (g) => <SyntheticBadge>{humanize(g.synthetic_status)}</SyntheticBadge> },
];

const selectClass = "min-h-9 rounded-sm border border-line-strong bg-surface px-2.5 text-sm text-ink";

export default function CareGapsPage() {
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [priority, setPriority] = useState("");
  const res = useApi(`care-gaps:${page}:${status}:${priority}`, (signal) =>
    apiList<CareGap>("/care-gaps", { page, page_size: PAGE_SIZE, status, priority }, signal),
  );
  const meta = res.data?.meta;
  const filtered = !!(status || priority);

  return (
    <>
      <PageHeader
        title="Care Gaps"
        description="Preventive care that is missing or overdue under approved guidelines. Open a case to see the rule version that raised it and its status history."
        actions={
          <>
            <label htmlFor="gap-status" className="font-medium">
              Status
            </label>
            <select
              id="gap-status"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setPage(1);
              }}
              className={selectClass}
            >
              <option value="">All</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {humanize(s)}
                </option>
              ))}
            </select>
            <label htmlFor="gap-priority" className="font-medium">
              Priority
            </label>
            <select
              id="gap-priority"
              value={priority}
              onChange={(e) => {
                setPriority(e.target.value);
                setPage(1);
              }}
              className={selectClass}
            >
              <option value="">All</option>
              {PRIORITIES.map((s) => (
                <option key={s} value={s}>
                  {humanize(s)}
                </option>
              ))}
            </select>
            <button type="button" className={btnSecondary} onClick={res.reload} disabled={res.loading}>
              Refresh
            </button>
          </>
        }
      />
      <DataTable
        caption="Care-gap cases"
        columns={columns}
        rows={res.data?.data ?? null}
        getRowId={(g) => g.id}
        loading={res.loading}
        error={res.error}
        onRetry={res.reload}
        unavailable={{
          endpoint: "GET /api/v1/care-gaps",
          willShow: [
            "Cases by status and priority, each linked to the guideline and rule version that raised it.",
            "The supporting evidence for each case and its full status history.",
          ],
        }}
        emptyTitle={filtered ? "No cases match these filters" : "No care-gap cases yet"}
        emptyHint={filtered ? "Clear a filter to see more cases." : "Cases are created when approved rules are evaluated against ingested residents."}
        page={meta?.page ?? page}
        pageSize={meta?.page_size ?? PAGE_SIZE}
        total={meta?.total}
        totalPages={meta?.total_pages}
        onPageChange={setPage}
      />
    </>
  );
}
