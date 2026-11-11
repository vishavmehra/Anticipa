"use client";

import Link from "next/link";
import { useState } from "react";
import { apiList } from "@/lib/api";
import { fmtDateTime, fmtInt } from "@/lib/format";
import { JOB_STATUSES, type Job } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { DataTable, type Column } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { PageHeader, ProgressBar, btnSecondary } from "@/components/ui";

const PAGE_SIZE = 25;

const columns: Column<Job>[] = [
  {
    key: "job",
    header: "Job",
    cell: (j) => (
      <>
        <Link href={`/jobs/${j.id}`} className="font-medium text-link underline underline-offset-2">
          {j.job_type}
        </Link>
        <p className="font-mono text-xs text-ink-muted">{j.id.slice(0, 8)}</p>
      </>
    ),
  },
  { key: "status", header: "Status", cell: (j) => <StatusBadge status={j.status} /> },
  {
    key: "progress",
    header: "Progress",
    className: "w-56",
    cell: (j) => (
      <>
        <ProgressBar
          done={j.processed_items}
          total={j.total_items}
          label={`${j.job_type} progress`}
          tone={j.status === "failed" ? "danger" : j.status === "completed" ? "ok" : j.status === "partially_completed" ? "warn" : "info"}
        />
        <p className="tnum text-xs text-ink-muted">
          {fmtInt(j.processed_items)} of {fmtInt(j.total_items)}
        </p>
      </>
    ),
  },
  { key: "failed", header: "Failed", align: "right", cell: (j) => fmtInt(j.failed_items) },
  { key: "started", header: "Started", className: "whitespace-nowrap tnum", cell: (j) => fmtDateTime(j.started_at ?? j.created_at) },
  { key: "updated", header: "Updated", className: "whitespace-nowrap tnum", cell: (j) => fmtDateTime(j.updated_at) },
];

export default function JobsPage() {
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const jobs = useApi(`jobs:${page}:${status}`, (signal) =>
    apiList<Job>("/jobs", { page, page_size: PAGE_SIZE, status }, signal),
  );
  const meta = jobs.data?.meta;

  return (
    <>
      <PageHeader
        title="Jobs"
        description="Background processing: ingestion, evidence extraction and rule evaluation. Open a job to follow its progress."
        actions={
          <>
            <label htmlFor="job-status" className="font-medium">
              Status
            </label>
            <select
              id="job-status"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setPage(1);
              }}
              className="min-h-9 rounded-sm border border-line-strong bg-surface px-2.5 text-sm text-ink"
            >
              <option value="">All</option>
              {JOB_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase())}
                </option>
              ))}
            </select>
            <button type="button" className={btnSecondary} onClick={jobs.reload} disabled={jobs.loading}>
              Refresh
            </button>
          </>
        }
      />
      <DataTable
        caption="Background jobs"
        columns={columns}
        rows={jobs.data?.data ?? null}
        getRowId={(j) => j.id}
        loading={jobs.loading}
        error={jobs.error}
        onRetry={jobs.reload}
        emptyTitle={status ? "No jobs with this status" : "No jobs have run yet"}
        emptyHint={status ? "Choose another status or clear the filter." : "Jobs appear here when ingestion, evidence extraction or evaluation is submitted."}
        page={meta?.page ?? page}
        pageSize={meta?.page_size ?? PAGE_SIZE}
        total={meta?.total}
        totalPages={meta?.total_pages}
        onPageChange={setPage}
      />
    </>
  );
}
