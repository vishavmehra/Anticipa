import type { ReactNode } from "react";
import { isMissingEndpoint, type ApiError } from "@/lib/api";
import { btnSecondary, EmptyState, ErrorPanel, Unavailable } from "./ui";

export interface Column<T> {
  key: string;
  header: string;
  cell: (row: T) => ReactNode;
  align?: "left" | "right";
  className?: string;
}

interface Props<T> {
  /** Accessible name; also the region label for keyboard scrolling. */
  caption: string;
  columns: readonly Column<T>[];
  rows: readonly T[] | null;
  getRowId: (row: T) => string;
  loading: boolean;
  error: ApiError | null;
  onRetry?: () => void;
  /** When the endpoint is absent from the backend build, show this instead of an error. */
  unavailable?: { endpoint: string; willShow?: readonly ReactNode[] };
  emptyTitle?: string;
  emptyHint?: ReactNode;
  /** Server pagination (contract §2: page, page_size, total, total_pages). */
  page?: number;
  pageSize?: number;
  total?: number;
  totalPages?: number;
  onPageChange?: (page: number) => void;
  /** Narrowest the table may get before its container scrolls sideways. Default 40rem; use "0" for 2 or 3 short columns. */
  minWidth?: string;
}

export function DataTable<T>({
  caption,
  columns,
  rows,
  getRowId,
  loading,
  error,
  onRetry,
  unavailable,
  emptyTitle = "Nothing to show yet",
  emptyHint,
  page = 1,
  pageSize,
  total,
  totalPages,
  onPageChange,
  minWidth = "40rem",
}: Props<T>) {
  if (error) {
    return unavailable && isMissingEndpoint(error) ? <Unavailable {...unavailable} /> : <ErrorPanel error={error} onRetry={onRetry} />;
  }

  if (!loading && rows && rows.length === 0) {
    return <EmptyState title={emptyTitle}>{emptyHint}</EmptyState>;
  }

  const pages = Math.max(1, totalPages ?? 1); // the API reports 0 pages for 0 rows
  const first = rows && rows.length && pageSize ? (page - 1) * pageSize + 1 : 0;
  const last = rows && pageSize ? (page - 1) * pageSize + rows.length : 0;

  return (
    <div className="glass-strong overflow-hidden">
      <div role="region" aria-label={caption} tabIndex={0} className="relative overflow-x-auto" aria-busy={loading}>
        <table className="w-full border-collapse text-left" style={{ minWidth }}>
          <caption className="sr-only">{caption}</caption>
          <thead>
            <tr className="border-b border-line bg-brand-soft/60">
              {columns.map((c) => (
                <th
                  key={c.key}
                  scope="col"
                  className={`px-4 py-3 text-[0.8125rem] font-semibold text-ink-soft ${c.align === "right" ? "text-right" : ""}`}
                >
                  {c.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className={loading && rows ? "opacity-60" : ""}>
            {!rows
              ? Array.from({ length: 6 }, (_, i) => (
                  <tr key={i} className="border-b border-line last:border-0">
                    {columns.map((c) => (
                      <td key={c.key} className="px-4 py-3.5">
                        <div className="skeleton h-4 w-3/4" />
                      </td>
                    ))}
                  </tr>
                ))
              : rows.map((row) => (
                  <tr key={getRowId(row)} className="border-b border-line last:border-0 hover:bg-brand-soft/40">
                    {columns.map((c) => (
                      <td
                        key={c.key}
                        className={`px-4 py-3 align-top ${c.align === "right" ? "tnum text-right" : ""} ${c.className ?? ""}`}
                      >
                        {c.cell(row)}
                      </td>
                    ))}
                  </tr>
                ))}
          </tbody>
        </table>
      </div>
      {!rows && <p className="sr-only" role="status">Loading</p>}
      {rows && onPageChange && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-4 py-3">
          <p role="status" className="tnum text-ink-muted">
            {total !== undefined ? `Showing ${first} to ${last} of ${total.toLocaleString("en-US")}` : `Page ${page}`}
          </p>
          <div className="flex items-center gap-2">
            <span className="tnum text-ink-muted">
              Page {page.toLocaleString("en-US")} of {pages.toLocaleString("en-US")}
            </span>
            <button type="button" className={btnSecondary} disabled={loading || page <= 1} onClick={() => onPageChange(page - 1)}>
              Previous
            </button>
            <button type="button" className={btnSecondary} disabled={loading || page >= pages} onClick={() => onPageChange(page + 1)}>
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
