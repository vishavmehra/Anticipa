"use client";

import { useState } from "react";
import { apiList } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import type { AuditEvent } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { DataTable, type Column } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { PageHeader, btnSecondary } from "@/components/ui";

const PAGE_SIZE = 50;

const columns: Column<AuditEvent>[] = [
  { key: "when", header: "When", className: "whitespace-nowrap tnum", cell: (e) => fmtDateTime(e.occurred_at) },
  {
    key: "actor",
    header: "Actor",
    cell: (e) => (
      <>
        <p>{e.actor_role ?? "system"}</p>
        {e.actor_id && <p className="font-mono text-xs text-ink-muted">{e.actor_id.slice(0, 8)}</p>}
      </>
    ),
  },
  { key: "action", header: "Action", cell: (e) => <span className="font-medium">{e.action}</span> },
  {
    key: "resource",
    header: "Resource",
    cell: (e) =>
      e.resource_type ? (
        <>
          <p>{e.resource_type}</p>
          {e.resource_id && <p className="font-mono text-xs text-ink-muted">{e.resource_id.slice(0, 8)}</p>}
        </>
      ) : (
        "None"
      ),
  },
  { key: "outcome", header: "Outcome", cell: (e) => <StatusBadge status={e.outcome} /> },
  {
    key: "details",
    header: "Details",
    cell: (e) =>
      e.details && Object.keys(e.details).length ? (
        <details>
          <summary className="cursor-pointer text-brand-ink underline underline-offset-2">View</summary>
          <pre className="mt-1 max-h-48 max-w-xs overflow-auto rounded-sm border border-line bg-canvas p-2 font-mono text-xs">
            {JSON.stringify(e.details, null, 2)}
          </pre>
        </details>
      ) : (
        <span className="text-ink-muted">None</span>
      ),
  },
  { key: "req", header: "Request", cell: (e) => <span className="font-mono text-xs">{e.request_id?.slice(0, 8) ?? "None"}</span> },
];

export default function AuditPage() {
  const [page, setPage] = useState(1);
  const audit = useApi(`audit:${page}`, (signal) => apiList<AuditEvent>("/audit", { page, page_size: PAGE_SIZE }, signal));
  const meta = audit.data?.meta;

  return (
    <>
      <PageHeader
        title="Audit"
        description="A permanent record of sensitive actions. Entries cannot be edited or deleted."
        actions={
          <button type="button" className={btnSecondary} onClick={audit.reload} disabled={audit.loading}>
            Refresh
          </button>
        }
      />
      <DataTable
        caption="Audit events"
        columns={columns}
        rows={audit.data?.data ?? null}
        getRowId={(e) => e.id}
        loading={audit.loading}
        error={audit.error}
        onRetry={audit.reload}
        emptyTitle="No audit events recorded"
        emptyHint="Events appear here as users and workers take audited actions."
        page={meta?.page ?? page}
        pageSize={meta?.page_size ?? PAGE_SIZE}
        total={meta?.total}
        totalPages={meta?.total_pages}
        onPageChange={setPage}
      />
    </>
  );
}
