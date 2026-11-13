"use client";

import { selectClass } from "@/components/ui";
import Link from "next/link";
import { useState } from "react";
import { apiList } from "@/lib/api";
import { fmtDateTime, humanize } from "@/lib/format";
import type { Guideline } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { DataTable, type Column } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { PageHeader, btnSecondary } from "@/components/ui";

const PAGE_SIZE = 25;
const STATUSES = ["draft", "in_review", "approved", "retired"] as const;

const vs = (g: Guideline) => g.versions ?? [];

const columns: Column<Guideline>[] = [
  {
    key: "title",
    header: "Guideline",
    cell: (g) => (
      <Link href={`/guidelines/${g.id}`} className="font-medium text-brand-ink underline underline-offset-2">
        {g.name}
      </Link>
    ),
  },
  { key: "topic", header: "Topic", cell: (g) => g.topic ?? "None" },
  { key: "source", header: "Authoritative source", cell: (g) => g.authoritative_source ?? <span className="text-ink-muted">Not recorded</span> },
  {
    key: "state",
    header: "Latest version",
    cell: (g) =>
      vs(g)[0] ? (
        <span className="inline-flex flex-wrap items-center gap-1.5">
          <span className="tnum">v{vs(g)[0].version}</span>
          <StatusBadge status={vs(g)[0].review_status} />
        </span>
      ) : (
        <span className="text-ink-muted">No versions</span>
      ),
  },
  {
    key: "approved",
    header: "Approved version",
    cell: (g) => {
      const a = vs(g).find((v) => v.review_status === "approved");
      return a ? <span className="tnum">v{a.version}</span> : <span className="text-ink-muted">None approved</span>;
    },
  },
  { key: "upd", header: "Updated", className: "whitespace-nowrap tnum", cell: (g) => fmtDateTime(g.updated_at) },
];

export default function GuidelinesPage() {
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const res = useApi(`guidelines:${page}:${status}`, (signal) => apiList<Guideline>("/guidelines", { page, page_size: PAGE_SIZE, status }, signal));
  const meta = res.data?.meta;

  return (
    <>
      <PageHeader
        title="Guidelines"
        description="Screening guidelines and their versions. Eligibility is decided by approved, versioned rules in code. AI reads documents; it never decides who is eligible."
        actions={
          <>
            <label htmlFor="guideline-status" className="font-medium">
              Version state
            </label>
            <select
              id="guideline-status"
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
            <button type="button" className={btnSecondary} onClick={res.reload} disabled={res.loading}>
              Refresh
            </button>
          </>
        }
      />
      <DataTable
        caption="Guidelines"
        columns={columns}
        rows={res.data?.data ?? null}
        getRowId={(g) => g.id}
        loading={res.loading}
        error={res.error}
        onRetry={res.reload}
        unavailable={{
          endpoint: "GET /api/v1/guidelines",
          willShow: [
            "Each guideline with its source, versions and approval state.",
            "The rules defined under each approved version.",
          ],
        }}
        emptyTitle={status ? "No guidelines with a version in this state" : "No guidelines have been loaded"}
        emptyHint={status ? "Choose another version state." : "Guidelines appear here once an authoritative source has been entered."}
        page={meta?.page ?? page}
        pageSize={meta?.page_size ?? PAGE_SIZE}
        total={meta?.total}
        totalPages={meta?.total_pages}
        onPageChange={setPage}
      />
    </>
  );
}
