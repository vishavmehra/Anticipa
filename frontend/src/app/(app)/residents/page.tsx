"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { apiList } from "@/lib/api";
import { LANGUAGES, ageYears, fmtInt, humanize } from "@/lib/format";
import type { Resident } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { SyntheticBadge } from "@/components/badges";
import { DataTable, type Column } from "@/components/data-table";
import { PageHeader, btnPrimary, btnSecondary, inputClass } from "@/components/ui";

const PAGE_SIZE = 25;

const columns: Column<Resident>[] = [
  {
    key: "resident",
    header: "Resident",
    cell: (r) => (
      <>
        <Link href={`/residents/${r.id}`} className="font-mono text-link underline underline-offset-2">
          {r.id.slice(0, 8)}
        </Link>
        {r.source_patient_id && <p className="font-mono text-xs break-all text-ink-muted">source {r.source_patient_id.slice(0, 13)}</p>}
      </>
    ),
  },
  { key: "age", header: "Age", align: "right", cell: (r) => fmtInt(ageYears(r.birth_date)) },
  { key: "sex", header: "Sex", cell: (r) => humanize(r.administrative_sex) },
  { key: "lang", header: "Language", cell: (r) => (r.preferred_language ? (LANGUAGES[r.preferred_language] ?? r.preferred_language) : "—") },
  { key: "district", header: "District", cell: (r) => r.district_code ?? "—" },
  { key: "gaps", header: "Open care gaps", align: "right", cell: (r) => fmtInt(r.open_care_gap_count) },
  { key: "label", header: "Data", cell: (r) => <SyntheticBadge>{humanize(r.synthetic_status)}</SyntheticBadge> },
];

export default function ResidentsPage() {
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState({ search: "", district_id: "" });
  const res = useApi(`residents:${page}:${filters.search}:${filters.district_id}`, (signal) =>
    apiList<Resident>("/residents", { page, page_size: PAGE_SIZE, ...filters }, signal),
  );
  const meta = res.data?.meta;

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setFilters({ search: String(f.get("search") ?? "").trim(), district_id: String(f.get("district_id") ?? "").trim() });
    setPage(1);
  }

  return (
    <>
      <PageHeader title="Residents" description="Look up synthetic residents and open their record, evidence and care-gap history." />
      <form role="search" aria-label="Find residents" onSubmit={onSubmit} className="mb-4 flex flex-wrap items-end gap-3">
        <div className="min-w-56 flex-1">
          <label htmlFor="resident-search" className="mb-1 block font-medium">
            Search
          </label>
          <input id="resident-search" name="search" type="search" className={inputClass} placeholder="Resident or source id" />
        </div>
        <div className="w-40">
          <label htmlFor="resident-district" className="mb-1 block font-medium">
            District
          </label>
          <input id="resident-district" name="district_id" className={inputClass} placeholder="District code" />
        </div>
        <button type="submit" className={btnPrimary}>
          Search
        </button>
        <button
          type="reset"
          className={btnSecondary}
          onClick={() => {
            setFilters({ search: "", district_id: "" });
            setPage(1);
          }}
        >
          Clear
        </button>
      </form>
      <DataTable
        caption="Residents"
        columns={columns}
        rows={res.data?.data ?? null}
        getRowId={(r) => r.id}
        loading={res.loading}
        error={res.error}
        onRetry={res.reload}
        unavailable={{
          endpoint: "GET /api/v1/residents",
          willShow: [
            "A searchable, paginated resident list within your permitted scope.",
            "Each resident's clinical records, documents and family links, labelled Synthetic.",
          ],
        }}
        emptyTitle={filters.search || filters.district_id ? "No residents match these filters" : "No residents have been ingested yet"}
        emptyHint={
          filters.search || filters.district_id
            ? "Change or clear the filters."
            : "Residents appear here after synthetic data generation and ingestion jobs complete."
        }
        page={meta?.page ?? page}
        pageSize={meta?.page_size ?? PAGE_SIZE}
        total={meta?.total}
        totalPages={meta?.total_pages}
        onPageChange={setPage}
      />
    </>
  );
}
