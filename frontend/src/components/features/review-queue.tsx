"use client";

import Link from "next/link";
import { useState } from "react";
import { apiList } from "@/lib/api";
import { humanize } from "@/lib/format";
import type { EvidenceFact } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { EvidenceCard } from "@/components/evidence-card";
import { StatusBadge } from "@/components/status-badge";
import { AsyncBlock, Card, CardHeader, Pager, btnSecondary, selectClass, Icon } from "@/components/ui";

const PAGE_SIZE = 12;
const FILTERS = [
  ["requires_review", "Needs review"],
  ["accepted", "Accepted"],
  ["rejected", "Rejected"],
  ["", "All facts"],
] as const;

export function ReviewQueue() {
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<string>("requires_review");
  const [picked, setPicked] = useState<string | null>(null);
  const res = useApi(`evidence:${page}:${status}`, (signal) =>
    apiList<EvidenceFact>("/evidence", { page, page_size: PAGE_SIZE, validation_status: status }, signal),
  );
  const rows = res.data?.data ?? [];
  const meta = res.data?.meta;
  const selected = rows.find((f) => f.id === picked) ?? rows[0] ?? null;

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-[70ch] text-ink-muted">Check facts the AI extracted from clinical notes. Every fact shows the exact quote it came from. Only facts whose quote is verified against the note can be accepted.</p>
        <button type="button" className={btnSecondary} onClick={res.reload} disabled={res.loading}>
          <Icon name="refresh" className="size-4" />
          Refresh
        </button>
      </div>
      <AsyncBlock
        loading={res.loading && !res.data}
        error={res.error}
        onRetry={res.reload}
        endpoint="GET /api/v1/evidence"
        empty={rows.length === 0}
        emptyTitle={status === "requires_review" ? "Nothing is waiting for review" : "No evidence matches this filter"}
        emptyHint="Facts appear here after clinical notes are processed by the extraction job."
      >
        <div className="grid gap-5 lg:grid-cols-[22rem_minmax(0,1fr)] [&>*]:min-w-0">
          <Card as="section" aria-labelledby="queue-title" className="self-start">
            <CardHeader
              id="queue-title"
              title={`Review queue (${(meta?.total ?? rows.length).toLocaleString("en-US")})`}
              aside={
                <>
                  <label htmlFor="evidence-status" className="sr-only">
                    Show
                  </label>
                  <select
                    id="evidence-status"
                    value={status}
                    onChange={(e) => {
                      setStatus(e.target.value);
                      setPage(1);
                      setPicked(null);
                    }}
                    className={`${selectClass} min-h-9 w-40`}
                  >
                    {FILTERS.map(([v, l]) => (
                      <option key={v} value={v}>
                        {l}
                      </option>
                    ))}
                  </select>
                </>
              }
            />
            <ul className="max-h-[34rem] space-y-2 overflow-y-auto px-3 pb-3" aria-busy={res.loading}>
              {rows.map((f) => {
                const on = selected?.id === f.id;
                return (
                  <li key={f.id}>
                    <button
                      type="button"
                      aria-pressed={on}
                      onClick={() => setPicked(f.id)}
                      className={`w-full rounded-control border px-3.5 py-3 text-left transition-colors ${on ? "border-brand bg-brand-soft" : "border-line bg-surface hover:bg-canvas"}`}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="font-mono text-[0.8125rem] font-semibold">{f.resident_id.slice(0, 8)}</span>
                        <StatusBadge status={f.assertion_status} />
                      </span>
                      <span className="mt-1 block font-semibold">{humanize(f.fact_type)}</span>
                      <span className="block text-[0.8125rem] text-ink-muted">
                        {humanize(f.category)}
                        {f.subject_type === "family_member" && f.relationship_type ? `, ${humanize(f.relationship_type)}` : ""}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
            <div className="border-t border-line px-4 pb-3">
              <Pager page={meta?.page ?? page} totalPages={meta?.total_pages} total={meta?.total} loading={res.loading} onPage={(p) => { setPage(p); setPicked(null); }} />
            </div>
          </Card>

          {selected && (
            <section aria-label="Selected fact" className="space-y-3">
              <p className="flex flex-wrap items-center gap-2 text-ink-muted">
                Resident
                <Link href={`/residents/${selected.resident_id}`} className="font-mono font-semibold text-brand-ink underline underline-offset-2">
                  {selected.resident_id.slice(0, 8)}
                </Link>
                <Link href={`/residents/${selected.resident_id}/journey`} className="font-semibold text-brand-ink underline underline-offset-2">
                  Open journey
                </Link>
              </p>
              <EvidenceCard key={selected.id} fact={selected} onReview={selected.validation_status === "requires_review" ? res.reload : undefined} />
            </section>
          )}
        </div>
      </AsyncBlock>
    </>
  );
}
