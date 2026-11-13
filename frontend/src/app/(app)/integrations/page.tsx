"use client";

import { api } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import type { Integration } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { DataTable, type Column } from "@/components/data-table";
import { StatTile } from "@/components/stat-tile";
import { StatusBadge } from "@/components/status-badge";
import { PageHeader, btnSecondary } from "@/components/ui";
import { displayText } from "@/lib/display";

const columns: Column<Integration>[] = [
  {
    key: "name",
    header: "Integration",
    cell: (i) => (
      <>
        <p className="font-medium">{i.name}</p>
        <p className="font-mono text-xs text-ink-muted">{i.key}</p>
      </>
    ),
  },
  { key: "status", header: "Status", cell: (i) => <StatusBadge status={i.status} /> },
  { key: "detail", header: "Detail", cell: (i) => displayText(i.detail) || <span className="text-ink-muted">None</span> },
  {
    key: "latency",
    header: "Latency",
    align: "right",
    cell: (i) => (i.latency_ms == null ? "None" : `${Math.round(i.latency_ms)} ms`),
  },
  { key: "checked", header: "Last checked", className: "whitespace-nowrap tnum", cell: (i) => fmtDateTime(i.checked_at) },
];

export default function IntegrationsPage() {
  const res = useApi("integrations", (signal) => api<Integration[]>("/integrations", { signal }));
  const list = res.data?.data ?? null;
  const loading = res.loading && !list;
  const count = (...s: string[]) => (list ? list.filter((i) => s.includes(i.status)).length : null);

  return (
    <>
      <PageHeader
        title="Integrations"
        description="Health of the services Anticipa depends on. Checks are lightweight connectivity tests and never use model quota."
        actions={
          <button type="button" className={btnSecondary} onClick={res.reload} disabled={res.loading}>
            Refresh
          </button>
        }
      />
      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile loading={loading} icon="check-circle" tint="green" label="Healthy" value={count("healthy")} basis="integrations reporting healthy" />
        <StatTile loading={loading} icon="warning" tint="amber" label="Degraded" value={count("degraded")} basis="integrations reporting degraded" />
        <StatTile loading={loading} icon="x-circle" tint="rose" label="Needs attention" value={count("unavailable", "authentication_failed")} basis="unavailable or authentication failed" />
        <StatTile loading={loading} icon="plug" tint="blue" label="Not configured" value={count("not_configured", "unknown")} basis="not configured or unknown" />
      </div>
      <DataTable
        caption="Integration health"
        columns={columns}
        rows={list}
        getRowId={(i) => i.key}
        loading={res.loading}
        error={res.error}
        onRetry={res.reload}
        emptyTitle="No integrations are registered"
        emptyHint="The backend returned no integrations."
      />
    </>
  );
}
