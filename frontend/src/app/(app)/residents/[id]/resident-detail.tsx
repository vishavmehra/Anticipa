"use client";

import Link from "next/link";
import { useState } from "react";
import { api, apiList } from "@/lib/api";
import { LANGUAGES, ageBandLabel, fmtDate, fmtInt, humanize } from "@/lib/format";
import type { CareGap, ClinicalRecord, EvidenceFact, Family, FamilyEdge, ResidentDetail, TimelineEntry } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { ConsentSection } from "@/components/consent";
import { DataTable, type Column } from "@/components/data-table";
import { EvidenceCard } from "@/components/evidence-card";
import { StatusBadge } from "@/components/status-badge";
import { Timeline, type TimelineItem } from "@/components/timeline";
import { AsyncBlock, ErrorPanel, Facts, Icon, PageHeader, Pager, Section, btnSecondary } from "@/components/ui";
import { displayText } from "@/lib/display";

const familyCols: Column<FamilyEdge>[] = [
  { key: "rel", header: "Their relationship to this resident", cell: (f) => humanize(f.relationship_type) },
  {
    key: "who",
    header: "Relative",
    cell: (f) =>
      f.related_resident ? (
        <>
          <Link href={`/residents/${f.related_resident.id}`} className="font-mono text-brand-ink underline underline-offset-2">
            {f.related_resident.id.slice(0, 8)}
          </Link>
          <p className="text-xs text-ink-muted">
            {[f.related_resident.age != null ? `${f.related_resident.age} years` : null, humanize(f.related_resident.administrative_sex), f.related_resident.district_code]
              .filter((x) => x && x !== "None")
              .join(", ")}
          </p>
        </>
      ) : (
        <span className="text-ink-muted">Not a resident{f.related_person_source_id ? ` (source ${f.related_person_source_id.slice(0, 13)})` : ""}</span>
      ),
  },
  { key: "src", header: "Source", cell: (f) => humanize(f.source_type) },
  { key: "conf", header: "Confidence", cell: (f) => <StatusBadge status={f.confidence_status} /> },
];

const gapCols: Column<CareGap>[] = [
  {
    key: "activity",
    header: "Care activity",
    cell: (g) => (
      <Link href={`/care-gaps/${g.id}`} className="font-medium text-brand-ink underline underline-offset-2">
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
    cell: (r) => (r.code ? <span className="font-mono text-xs">{[r.code_system?.split("/").pop(), r.code].filter(Boolean).join(" ")}</span> : "None"),
  },
  {
    key: "display",
    header: "Description",
    cell: (r) => (
      <>
        {displayText(r.display) || "None"}
        {r.value && Object.keys(r.value).length > 0 && (
          <details className="mt-1">
            <summary className="cursor-pointer text-xs text-brand-ink underline underline-offset-2">Value</summary>
            <pre className="mt-1 max-h-40 max-w-xs overflow-auto rounded-sm border border-line bg-canvas p-2 font-mono text-xs">{JSON.stringify(r.value, null, 2)}</pre>
          </details>
        )}
      </>
    ),
  },
  { key: "status", header: "Status", cell: (r) => humanize(r.status) },
  { key: "ref", header: "Source", className: "min-w-56", cell: (r) => <span className="font-mono text-xs break-all">{r.source_reference}</span> },
];

const countCols: Column<{ category: string; count: number }>[] = [
  { key: "cat", header: "Category", cell: (r) => humanize(r.category) },
  { key: "n", header: "Records", align: "right", cell: (r) => fmtInt(r.count) },
];

export function Records({ id }: { id: string }) {
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

export function FamilyBlock({ id }: { id: string }) {
  const res = useApi(`family:${id}`, (signal) => api<Family>(`/residents/${id}/family`, { signal }));
  const f = res.data?.data;
  return (
    <>
      {f && f.edges.length > 0 && <p className="mb-2 max-w-[65ch] text-ink-muted">Each row shows how the relative is related to this resident.</p>}
      <DataTable
        caption="Family relationships"
        columns={familyCols}
        rows={f ? f.edges : null}
        getRowId={(e) => e.id}
        loading={res.loading}
        error={res.error}
        onRetry={res.reload}
        unavailable={{ endpoint: "GET /api/v1/residents/{id}/family" }}
        emptyTitle="No family links recorded"
        emptyHint="Links come from the family linker or from verified family history evidence."
      />
    </>
  );
}

export function CareGaps({ id }: { id: string }) {
  const res = useApi(`resident-gaps:${id}`, (signal) => apiList<CareGap>("/care-gaps", { resident_id: id, page_size: 50 }, signal));
  return (
    <DataTable
      caption="Care gaps"
      columns={gapCols}
      rows={res.data?.data ?? null}
      getRowId={(g) => g.id}
      loading={res.loading}
      error={res.error}
      onRetry={res.reload}
      unavailable={{ endpoint: "GET /api/v1/care-gaps" }}
      emptyTitle="No care gaps for this resident"
      emptyHint="No preventive care has been raised for this resident."
    />
  );
}

export function TimelineBlock({ id }: { id: string }) {
  const res = useApi(`timeline:${id}`, (signal) => apiList<TimelineEntry>(`/residents/${id}/timeline`, { page_size: 100 }, signal));
  const items: TimelineItem[] = (res.data?.data ?? []).map((e) => ({
    id: e.event_id,
    at: e.occurred_at,
    tone: e.source === "document" ? "info" : "neutral",
    title: (
      <>
        {humanize(e.kind)}
        {e.title && e.title !== e.kind ? `: ${displayText(e.title)}` : ""}
      </>
    ),
    detail: (
      <>
        {e.source === "document" ? "Registered document" : "Clinical record"}
        {e.status ? `, ${humanize(e.status)}` : ""}
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
      emptyHint="Records and documents appear here newest first, each with its source reference."
    >
      <div className="card p-5">
        <Timeline label="Resident timeline" items={items} />
        {(res.data?.meta.total ?? 0) > items.length && <p className="mt-3 text-ink-muted">Showing the newest {items.length} of {fmtInt(res.data?.meta.total)} entries.</p>}
      </div>
    </AsyncBlock>
  );
}

export function EvidenceBlock({ id }: { id: string }) {
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
      <Link href="/residents" className="text-brand-ink underline underline-offset-2">
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

  const counts = Object.entries(r.record_counts).map(([category, count]) => ({ category, count }));

  return (
    <>
      {back}
      <PageHeader
        title={`Resident ${r.id.slice(0, 8)}`}
        description={<span className="font-mono text-xs">{r.id}</span>}
        actions={
          <Link href={`/residents/${r.id}/journey`} className={btnSecondary}>
            <Icon name="activity" className="size-4" />
            Open care journey
          </Link>
        }
      />

      <div className="grid gap-6 xl:grid-cols-[22rem_minmax(0,1fr)] [&>*]:min-w-0">
        <div>
          <Section id="overview" title="Resident summary">
            <Facts
              items={[
                ["Age", r.age == null ? null : `${r.age} years, on ${fmtDate(r.age_as_of)}`],
                ["Age band", ageBandLabel(r.age_band)],
                ["Birth date", fmtDate(r.birth_date)],
                ["Sex", humanize(r.administrative_sex)],
                ["Preferred language", r.preferred_language ? (LANGUAGES[r.preferred_language] ?? r.preferred_language) : null],
                ["District", r.district_code],
                ["Deceased", r.deceased_date ? fmtDate(r.deceased_date) : "No"],
                ["Source patient", <span key="s" className="font-mono text-xs break-all">{r.source_patient_id}</span>],
                ["Dataset", r.dataset_version],
                ["Family links", fmtInt(r.family_edge_count)],
                ["Registered documents", fmtInt(r.document_count)],
              ]}
            />
          </Section>
          <Section id="counts" title="Records by category">
            <DataTable caption="Record counts by category" columns={countCols} rows={counts} getRowId={(c) => c.category} loading={false} error={null} emptyTitle="No clinical records loaded" minWidth="0" />
          </Section>
        </div>

        <div>
          <Section id="gaps" title="Care gaps">
            <CareGaps id={id} />
          </Section>
          <Section id="consent" title="Consent">
            <ConsentSection residentId={id} />
          </Section>
          <Section id="family" title="Family">
            <FamilyBlock id={id} />
          </Section>
          <Section id="timeline" title="Timeline" aside={<span className="text-xs text-ink-muted">Times in UTC, newest first</span>}>
            <TimelineBlock id={id} />
          </Section>
          <Section id="evidence" title="Evidence">
            <EvidenceBlock id={id} />
          </Section>
          <Section id="records" title="Clinical records">
            <Records id={id} />
          </Section>
        </div>
      </div>
    </>
  );
}
