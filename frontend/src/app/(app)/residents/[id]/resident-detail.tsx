"use client";

import Link from "next/link";
import { useState } from "react";
import { api, apiList } from "@/lib/api";
import { LANGUAGES, ageYears, fmtDate, fmtInt, humanize } from "@/lib/format";
import type { CareGap, ClinicalRecord, EvidenceFact, FamilyLink, ResidentDetail, TimelineEntry } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { SyntheticBadge } from "@/components/badges";
import { DataTable, type Column } from "@/components/data-table";
import { EvidenceCard } from "@/components/evidence-card";
import { StatusBadge } from "@/components/status-badge";
import { Timeline, type TimelineItem } from "@/components/timeline";
import { AsyncBlock, ErrorPanel, Facts, PageHeader, Pager, Section } from "@/components/ui";

const familyCols: Column<FamilyLink>[] = [
  { key: "rel", header: "Relationship", cell: (f) => humanize(f.relationship_type) },
  {
    key: "who",
    header: "Relative",
    cell: (f) =>
      f.related_resident_id ? (
        <Link href={`/residents/${f.related_resident_id}`} className="font-mono text-link underline underline-offset-2">
          {f.related_resident_id.slice(0, 8)}
        </Link>
      ) : (
        <span className="text-ink-muted">Not a resident{f.related_person_source_id ? ` (source ${f.related_person_source_id.slice(0, 13)})` : ""}</span>
      ),
  },
  { key: "src", header: "Source", cell: (f) => humanize(f.source_type) },
  { key: "conf", header: "Confidence", cell: (f) => <StatusBadge status={f.confidence_status} /> },
  { key: "label", header: "Data", cell: (f) => <SyntheticBadge>{humanize(f.synthetic_status)}</SyntheticBadge> },
];

const gapCols: Column<CareGap>[] = [
  {
    key: "activity",
    header: "Care activity",
    cell: (g) => (
      <Link href={`/care-gaps/${g.id}`} className="font-medium text-link underline underline-offset-2">
        {humanize(g.care_activity)}
      </Link>
    ),
  },
  { key: "status", header: "Status", cell: (g) => <StatusBadge status={g.status} /> },
  { key: "priority", header: "Priority", cell: (g) => humanize(g.priority) },
];

const recordCols: Column<ClinicalRecord>[] = [
  { key: "date", header: "Date", className: "whitespace-nowrap tnum", cell: (r) => fmtDate(r.effective_start) },
  { key: "cat", header: "Category", cell: (r) => humanize(r.category) },
  { key: "type", header: "Resource", cell: (r) => r.resource_type },
  {
    key: "code",
    header: "Code",
    cell: (r) => (r.code ? <span className="font-mono text-xs">{[r.code_system?.split("/").pop(), r.code].filter(Boolean).join(" ")}</span> : "—"),
  },
  { key: "display", header: "Description", cell: (r) => r.normalized_display ?? r.display ?? "—" },
  { key: "status", header: "Status", cell: (r) => humanize(r.status) },
  { key: "ref", header: "Source", cell: (r) => <span className="font-mono text-xs break-all">{r.source_reference}</span> },
  { key: "label", header: "Data", cell: (r) => <SyntheticBadge>{humanize(r.synthetic_status)}</SyntheticBadge> },
];

const summaryCols: Column<{ category: string; count: number }>[] = [
  { key: "cat", header: "Category", cell: (r) => humanize(r.category) },
  { key: "n", header: "Records", align: "right", cell: (r) => fmtInt(r.count) },
];

function Records({ id }: { id: string }) {
  const [page, setPage] = useState(1);
  const res = useApi(`records:${id}:${page}`, (signal) => apiList<ClinicalRecord>(`/residents/${id}/records`, { page, page_size: 25 }, signal));
  const meta = res.data?.meta;
  return (
    <DataTable
      caption="Clinical records"
      columns={recordCols}
      rows={res.data?.data ?? null}
      getRowId={(r) => r.id}
      loading={res.loading}
      error={res.error}
      onRetry={res.reload}
      unavailable={{ endpoint: "GET /api/v1/residents/{id}/records" }}
      emptyTitle="No clinical records for this resident"
      page={meta?.page ?? page}
      pageSize={meta?.page_size ?? 25}
      total={meta?.total}
      totalPages={meta?.total_pages}
      onPageChange={setPage}
    />
  );
}

function TimelineBlock({ id }: { id: string }) {
  const res = useApi(`timeline:${id}`, (signal) => apiList<TimelineEntry>(`/residents/${id}/timeline`, { page_size: 100 }, signal));
  const items: TimelineItem[] = (res.data?.data ?? []).map((e) => ({
    id: e.id,
    at: e.occurred_at,
    tone: e.kind === "evidence" ? "info" : "neutral",
    title: (
      <>
        {humanize(e.kind)}: {e.title}
      </>
    ),
    detail: (
      <>
        {e.detail}
        {e.source_reference && <span className="block font-mono text-xs break-all">{e.source_reference}</span>}
      </>
    ),
  }));
  return (
    <AsyncBlock
      loading={res.loading}
      error={res.error}
      onRetry={res.reload}
      endpoint="GET /api/v1/residents/{id}/timeline"
      empty={items.length === 0}
      emptyTitle="No timeline entries"
      emptyHint="Records and workflow events appear here in date order, each with its source reference."
    >
      <div className="rounded-md border border-line bg-surface p-4">
        <Timeline label="Resident timeline" items={items} />
        {(res.data?.meta.total ?? 0) > items.length && <p className="mt-3 text-ink-muted">Showing the first {items.length} of {fmtInt(res.data?.meta.total)} entries.</p>}
      </div>
    </AsyncBlock>
  );
}

function EvidenceBlock({ id }: { id: string }) {
  const [page, setPage] = useState(1);
  const res = useApi(`resident-evidence:${id}:${page}`, (signal) => apiList<EvidenceFact>(`/residents/${id}/evidence`, { page, page_size: 10 }, signal));
  const rows = res.data?.data ?? [];
  const meta = res.data?.meta;
  return (
    <AsyncBlock
      loading={res.loading && !res.data}
      error={res.error}
      onRetry={res.reload}
      endpoint="GET /api/v1/residents/{id}/evidence"
      empty={rows.length === 0}
      emptyTitle="No evidence extracted for this resident"
      emptyHint="Facts appear here once documents are processed, each with the exact quote it came from."
    >
      <div className="space-y-3">
        {rows.map((f) => (
          <EvidenceCard key={f.id} fact={f} />
        ))}
      </div>
      <Pager page={meta?.page ?? page} totalPages={meta?.total_pages} total={meta?.total} loading={res.loading} onPage={setPage} />
    </AsyncBlock>
  );
}

export function ResidentDetailView({ id }: { id: string }) {
  const res = useApi(`resident:${id}`, (signal) => api<ResidentDetail>(`/residents/${id}`, { signal }));
  const r = res.data?.data;
  const back = (
    <p className="mb-2">
      <Link href="/residents" className="text-link underline underline-offset-2">
        All residents
      </Link>
    </p>
  );

  if (res.error) {
    return (
      <>
        {back}
        <PageHeader title="Resident" />
        <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load this resident" />
      </>
    );
  }
  if (!r) return <p role="status" className="text-ink-muted">Loading resident…</p>;

  const age = ageYears(r.birth_date);
  const evidence = Object.entries(r.evidence_summary ?? {});

  return (
    <>
      {back}
      <PageHeader
        title={`Resident ${r.id.slice(0, 8)}`}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <SyntheticBadge>{humanize(r.synthetic_status)} resident</SyntheticBadge>
            <span className="font-mono text-xs">{r.id}</span>
          </span>
        }
      />

      <Section id="overview" title="Overview">
        <div className="grid gap-4 lg:grid-cols-2">
          <Facts
            items={[
              ["Age", age === null ? null : `${age} years`],
              ["Birth date", fmtDate(r.birth_date)],
              ["Sex", humanize(r.administrative_sex)],
              ["Preferred language", r.preferred_language ? (LANGUAGES[r.preferred_language] ?? r.preferred_language) : null],
              ["District", r.district_code],
              ["Deceased", r.deceased_date ? fmtDate(r.deceased_date) : "No"],
              ["Source", r.source_system ? `${r.source_system} ${r.source_patient_id ?? ""}` : r.source_patient_id],
            ]}
          />
          <div className="rounded-md border border-line bg-surface p-4">
            <h3 className="mb-2 font-medium">Evidence by validation status</h3>
            {evidence.length ? (
              <ul className="flex flex-wrap gap-x-4 gap-y-2">
                {evidence.map(([k, n]) => (
                  <li key={k} className="flex items-center gap-2">
                    <StatusBadge status={k} />
                    <span className="tnum font-medium">{fmtInt(n)}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-ink-muted">No evidence summary reported.</p>
            )}
          </div>
        </div>
      </Section>

      <Section id="gaps" title="Open care gaps">
        <DataTable caption="Open care gaps" columns={gapCols} rows={r.open_care_gaps ?? []} getRowId={(g) => g.id} loading={false} error={null} emptyTitle="No open care gaps" emptyHint="No guideline-defined preventive care is currently open for this resident." />
      </Section>

      <Section id="family" title="Family">
        <DataTable caption="Family relationships" columns={familyCols} rows={r.family ?? []} getRowId={(f) => f.id} loading={false} error={null} emptyTitle="No family links recorded" emptyHint="Links come from structured records or verified family-history evidence." />
      </Section>

      {r.record_summary && r.record_summary.length > 0 && (
        <Section id="summary" title="Records by category">
          <DataTable caption="Record counts by category" columns={summaryCols} rows={r.record_summary} getRowId={(s) => s.category} loading={false} error={null} />
        </Section>
      )}

      <Section id="records" title="Clinical records">
        <Records id={id} />
      </Section>

      <Section id="timeline" title="Timeline" aside={<span className="text-xs text-ink-muted">Times in UTC</span>}>
        <TimelineBlock id={id} />
      </Section>

      <Section id="evidence" title="Evidence">
        <EvidenceBlock id={id} />
      </Section>
    </>
  );
}
