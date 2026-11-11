"use client";

import { useState } from "react";
import { apiList } from "@/lib/api";
import type { EvidenceFact } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { EvidenceCard } from "@/components/evidence-card";
import { AsyncBlock, PageHeader, Pager, btnSecondary } from "@/components/ui";

const PAGE_SIZE = 10;
const FILTERS = [
  ["requires_review", "Requires review"],
  ["accepted", "Accepted"],
  ["rejected", "Rejected"],
  ["", "All"],
] as const;

export default function EvidenceReviewPage() {
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<string>("requires_review");
  const res = useApi(`evidence:${page}:${status}`, (signal) =>
    apiList<EvidenceFact>("/evidence", { page, page_size: PAGE_SIZE, validation_status: status }, signal),
  );
  const rows = res.data?.data ?? [];
  const meta = res.data?.meta;

  return (
    <>
      <PageHeader
        title="Evidence Review"
        description="Facts extracted from clinical documents that need a human decision. Each fact shows the exact source quote; only quotes verified against the source text can be accepted."
        actions={
          <>
            <label htmlFor="evidence-status" className="font-medium">
              Show
            </label>
            <select
              id="evidence-status"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setPage(1);
              }}
              className="min-h-9 rounded-sm border border-line-strong bg-surface px-2.5 text-sm text-ink"
            >
              {FILTERS.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </select>
            <button type="button" className={btnSecondary} onClick={res.reload} disabled={res.loading}>
              Refresh
            </button>
          </>
        }
      />
      <AsyncBlock
        loading={res.loading && !res.data}
        error={res.error}
        onRetry={res.reload}
        endpoint="GET /api/v1/evidence"
        empty={rows.length === 0}
        emptyTitle={status === "requires_review" ? "Nothing is waiting for review" : "No evidence matches this filter"}
        emptyHint="Facts appear here after documents are processed by the extraction job."
      >
        <div className="space-y-3" aria-busy={res.loading}>
          {rows.map((f) => (
            <EvidenceCard key={f.id} fact={f} showResident onReview={f.validation_status === "requires_review" ? res.reload : undefined} />
          ))}
        </div>
        <Pager page={meta?.page ?? page} totalPages={meta?.total_pages} total={meta?.total} loading={res.loading} onPage={setPage} />
      </AsyncBlock>
    </>
  );
}
