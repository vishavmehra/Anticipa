import type { ReactNode } from "react";
import { isMissingEndpoint, type ApiError } from "@/lib/api";

export const btn =
  "inline-flex min-h-9 items-center justify-center gap-1.5 rounded-sm border px-3 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50";
export const btnPrimary = `${btn} border-spruce bg-spruce text-white hover:bg-spruce-hover`;
export const btnSecondary = `${btn} border-line-strong bg-surface text-ink hover:bg-neutral-bg`;
export const btnOnDark = `${btn} border-[#4d6b76] bg-transparent text-white hover:bg-spruce-hover`;
export const btnDanger = `${btn} border-danger bg-surface text-danger hover:bg-danger-bg`;
export const inputClass =
  "min-h-9 w-full rounded-sm border border-line-strong bg-surface px-2.5 text-sm text-ink placeholder:text-ink-muted";

/** `total === null` means unknown size: shows the count only, never a made-up percentage. */
export function ProgressBar({
  done,
  total,
  label,
  tone = "info",
}: {
  done: number;
  total: number | null | undefined;
  label: string;
  tone?: "info" | "ok" | "danger" | "warn" | "neutral";
}) {
  const known = typeof total === "number" && total > 0;
  const value = known ? Math.min(100, Math.round((done / total) * 100)) : null;
  const fill = { info: "bg-info", ok: "bg-ok", danger: "bg-danger", warn: "bg-warn", neutral: "bg-neutral" }[tone];
  return (
    <div className="flex items-center gap-2">
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={known ? total : undefined}
        aria-valuenow={known ? Math.min(done, total) : undefined}
        aria-valuetext={known ? `${done} of ${total}` : `${done} processed, total unknown`}
        className="h-2 min-w-16 flex-1 overflow-hidden rounded-sm bg-neutral-bg"
      >
        {value !== null && <div className={`h-full ${fill} transition-[width] duration-500`} style={{ width: `${value}%` }} />}
      </div>
      <span className="tnum w-12 text-right text-xs text-ink-muted">{value !== null ? `${value}%` : "—"}</span>
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 max-w-[65ch] text-ink-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-line-strong bg-surface px-5 py-8">
      <h2 className="text-base font-semibold">{title}</h2>
      {children && <div className="mt-1.5 max-w-[65ch] text-ink-muted">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

const GUIDANCE: Partial<Record<ApiError["code"], string>> = {
  NETWORK_ERROR: "Check that the backend is running on http://localhost:8000, then retry.",
  FORBIDDEN: "Your role is not permitted to view this. Ask a population administrator if you need access.",
  UNAUTHENTICATED: "Your session has ended. Sign in again.",
  RATE_LIMITED: "Too many requests. Wait a moment, then retry.",
  PROVIDER_UNAVAILABLE: "An upstream provider is unavailable. Retry shortly.",
};

/** Inline, announced error with the contract's request_id so failures can be traced in audit/logs. */
export function ErrorPanel({ error, onRetry, title = "Could not load this data" }: { error: ApiError; onRetry?: () => void; title?: string }) {
  return (
    <div role="alert" className="rounded-md border border-danger bg-danger-bg px-4 py-3 text-danger">
      <p className="font-semibold">{title}</p>
      <p className="mt-0.5">{error.message}</p>
      {GUIDANCE[error.code] && <p className="mt-0.5">{GUIDANCE[error.code]}</p>}
      <p className="mt-1 text-xs">
        Code {error.code}
        {error.status ? `, HTTP ${error.status}` : ""}
        {error.requestId ? (
          <>
            , request <span className="font-mono">{error.requestId}</span>
          </>
        ) : null}
      </p>
      {onRetry && (
        <button type="button" onClick={onRetry} className={`${btnSecondary} mt-3`}>
          Retry
        </button>
      )}
    </div>
  );
}

/** The backend build does not expose this endpoint yet. Never shows sample figures. */
export function Unavailable({ endpoint, willShow }: { endpoint: string; willShow?: readonly ReactNode[] }) {
  return (
    <EmptyState title="Endpoint not yet available">
      <p>
        This page reads <code className="font-mono text-[0.9em]">{endpoint}</code>, which the backend does not expose in
        this build. No data is shown until it does.
      </p>
      {willShow && willShow.length > 0 && (
        <>
          <p className="mt-3 font-medium text-ink">When connected, this page will show:</p>
          <ul className="mt-1 list-disc pl-5">
            {willShow.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </>
      )}
    </EmptyState>
  );
}

/** Bordered titled block used on detail pages. */
export function Section({ id, title, aside, children }: { id: string; title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="mb-6">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 id={id} className="text-base font-semibold">
          {title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

/** Label/value pairs. A null or empty value renders an em dash, never a blank. */
export function Facts({ items }: { items: readonly [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 rounded-md border border-line bg-surface p-4">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-ink-muted">{k}</dt>
          <dd className="min-w-0 wrap-break-word">{v === null || v === undefined || v === "" ? "—" : v}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * Loading, unavailable-endpoint, error and empty states for a non-table block of API data.
 * Children render only when data has arrived and is not empty.
 */
export function AsyncBlock({
  loading,
  error,
  onRetry,
  endpoint,
  empty,
  emptyTitle,
  emptyHint,
  children,
}: {
  loading: boolean;
  error: ApiError | null;
  onRetry?: () => void;
  endpoint: string;
  empty: boolean;
  emptyTitle: string;
  emptyHint?: ReactNode;
  children: ReactNode;
}) {
  if (error) return isMissingEndpoint(error) ? <Unavailable endpoint={endpoint} /> : <ErrorPanel error={error} onRetry={onRetry} />;
  if (loading) {
    return (
      <div role="status" aria-label="Loading" className="space-y-2 rounded-md border border-line bg-surface p-4">
        <div className="skeleton h-4 w-3/4" />
        <div className="skeleton h-4 w-1/2" />
        <div className="skeleton h-4 w-2/3" />
      </div>
    );
  }
  if (empty) return <EmptyState title={emptyTitle}>{emptyHint}</EmptyState>;
  return <>{children}</>;
}

/** Previous/Next for blocks that are not a DataTable. Uses the contract page meta. */
export function Pager({ page, totalPages, total, loading, onPage }: { page: number; totalPages?: number; total?: number; loading: boolean; onPage: (p: number) => void }) {
  const pages = Math.max(1, totalPages ?? 1);
  return (
    <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
      <p role="status" className="tnum text-ink-muted">
        {total !== undefined ? `${total.toLocaleString("en-US")} in total. ` : ""}Page {page} of {pages}
      </p>
      <div className="flex gap-2">
        <button type="button" className={btnSecondary} disabled={loading || page <= 1} onClick={() => onPage(page - 1)}>
          Previous
        </button>
        <button type="button" className={btnSecondary} disabled={loading || page >= pages} onClick={() => onPage(page + 1)}>
          Next
        </button>
      </div>
    </div>
  );
}

/** Collapsed raw JSON for structures the UI does not model (eligibility reasoning, planning payloads). */
export function JsonBlock({ label, value }: { label: string; value: unknown }) {
  const empty = value == null || (typeof value === "object" && Object.keys(value).length === 0);
  if (empty) return <p className="text-ink-muted">{label}: none recorded.</p>;
  return (
    <details className="rounded-md border border-line bg-surface p-3">
      <summary className="cursor-pointer font-medium">{label}</summary>
      <pre className="mt-2 max-h-64 overflow-auto rounded-sm border border-line bg-paper p-2 font-mono text-xs">{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

export function Notice({ tone, children }: { tone: "ok" | "warn"; children: ReactNode }) {
  const c = tone === "ok" ? "border-ok bg-ok-bg text-ok" : "border-warn bg-warn-bg text-warn";
  return (
    <p role="status" className={`rounded-md border px-4 py-2 ${c}`}>
      {children}
    </p>
  );
}
