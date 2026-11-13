import type { ReactNode } from "react";
import { isMissingEndpoint, type ApiError } from "@/lib/api";
import { Icon, type IconName } from "./icons";
import { displayText } from "@/lib/display";

// ---- controls ---------------------------------------------------------------------------------------------------
export const btn =
  "inline-flex min-h-10 items-center justify-center gap-2 rounded-full border px-5 text-sm font-semibold whitespace-nowrap transition-[background-color,box-shadow,filter] disabled:cursor-not-allowed disabled:opacity-50";
export const btnPrimary = `${btn} grad-brand border-transparent text-white shadow-[0_8px_18px_-8px_rgb(79_51_168/0.75)] hover:brightness-110`;
export const btnSecondary = `${btn} border-line-strong/40 bg-[var(--control-bg)] text-ink shadow-sm hover:bg-surface`;
export const btnGhost = `${btn} border-transparent bg-transparent text-brand-ink hover:bg-brand-soft`;
export const btnOnDark = `${btn} border-navy-line bg-transparent text-white hover:bg-navy-2`;
export const btnDanger = `${btn} border-danger-line bg-[var(--control-bg)] text-danger hover:bg-danger-bg`;
export const inputClass =
  "min-h-10 w-full rounded-control border border-line-strong bg-[var(--control-bg)] px-3 text-sm text-ink placeholder:text-ink-muted";
export const selectClass = `${inputClass} select-arrow pr-9`;

// ---- progress -----------------------------------------------------------------------------------------------------
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
  tone?: "info" | "ok" | "danger" | "warn" | "neutral" | "brand";
}) {
  const known = typeof total === "number" && total > 0;
  const value = known ? Math.min(100, Math.round((done / total) * 100)) : null;
  const fill = { info: "bg-info", ok: "bg-ok", danger: "bg-danger", warn: "bg-warn", neutral: "bg-neutral", brand: "bg-brand-mid" }[tone];
  return (
    <div className="flex items-center gap-2">
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={known ? total : undefined}
        aria-valuenow={known ? Math.min(done, total) : undefined}
        aria-valuetext={known ? `${done} of ${total}` : `${done} processed, total unknown`}
        className="h-2.5 min-w-16 flex-1 overflow-hidden rounded-full bg-neutral-bg"
      >
        {value !== null && <div className={`h-full rounded-full ${fill} transition-[width] duration-500`} style={{ width: `${value}%` }} />}
      </div>
      <span className="tnum w-12 text-right text-xs text-ink-muted">{value !== null ? `${value}%` : "None"}</span>
    </div>
  );
}

// ---- page structure -----------------------------------------------------------------------------------------------
/** Small purple uppercase label above a headline. */
export function Eyebrow({ children }: { children: ReactNode }) {
  return <p className="eyebrow">{children}</p>;
}

/**
 * Page title block from the owner references: optional breadcrumb, purple eyebrow, serif headline, one line of
 * description. `tabs` sits on the right of the headline row (use PillTabs); `actions` follow it.
 */
export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
  crumbs,
  tabs,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: string;
  crumbs?: readonly string[];
  tabs?: ReactNode;
}) {
  return (
    <header className="mb-6">
      {crumbs && crumbs.length > 0 && (
        <nav aria-label="Breadcrumb" className="mb-2 flex flex-wrap items-center gap-1.5 text-sm text-ink-muted">
          {crumbs.map((c, i) => (
            <span key={`${i}-${c}`} className="flex items-center gap-1.5">
              {i > 0 && <Icon name="chevron-right" className="size-3.5" />}
              <span aria-current={i === crumbs.length - 1 ? "page" : undefined} className={i === crumbs.length - 1 ? "text-ink-soft" : ""}>
                {c}
              </span>
            </span>
          ))}
        </nav>
      )}
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
        <div className="min-w-0">
          {eyebrow && <Eyebrow>{eyebrow}</Eyebrow>}
          <h1 className="font-display text-[2.1rem] leading-[1.12] font-bold tracking-tight text-ink [overflow-wrap:anywhere] md:text-[2.4rem]">{title}</h1>
          {description && <p className="mt-1.5 max-w-[72ch] text-base text-ink-soft">{description}</p>}
        </div>
        {(tabs || actions) && (
          <div className="flex min-w-0 max-w-full flex-wrap items-center gap-3">
            {tabs}
            {actions}
          </div>
        )}
      </div>
    </header>
  );
}

/**
 * Pill tab group (glass track, gradient active pill). Roving focus with arrow keys. Render the panel yourself:
 * `<div role="tabpanel" id={`${idBase}-panel`} aria-labelledby={`${idBase}-tab-${value}`}>`.
 */
export function PillTabs<T extends string>({
  items,
  value,
  onChange,
  label,
  idBase,
}: {
  items: readonly { id: T; label: string; icon?: IconName }[];
  value: T;
  onChange: (id: T) => void;
  label: string;
  idBase: string;
}) {
  const move = (e: React.KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft" && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    const n = e.key === "Home" ? 0 : e.key === "End" ? items.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + items.length) % items.length;
    onChange(items[n].id);
    document.getElementById(`${idBase}-tab-${items[n].id}`)?.focus();
  };
  return (
    <div role="tablist" aria-label={label} className="glass-strong inline-flex max-w-full items-center gap-1 overflow-x-auto rounded-full p-1.5 scroll-quiet">
      {items.map((t, i) => {
        const on = t.id === value;
        return (
          <button
            key={t.id}
            id={`${idBase}-tab-${t.id}`}
            role="tab"
            type="button"
            aria-selected={on}
            aria-controls={on ? `${idBase}-panel` : undefined}
            tabIndex={on ? 0 : -1}
            onClick={() => onChange(t.id)}
            onKeyDown={(e) => move(e, i)}
            className={`inline-flex min-h-10 shrink-0 items-center gap-2 rounded-full px-4 text-sm font-semibold whitespace-nowrap transition-colors ${
              on ? "grad-brand text-white shadow-[0_6px_14px_-6px_rgb(79_51_168/0.8)]" : "text-ink-soft hover:bg-brand-soft hover:text-brand-ink"
            }`}
          >
            {t.icon && <Icon name={t.icon} className="size-4" />}
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

/** Row of filters above a page body. Children are <FilterField>s or any control. */
export function FilterBar({ children, label = "Filters" }: { children: ReactNode; label?: string }) {
  return (
    <div role="group" aria-label={label} className="card mb-5 flex flex-wrap items-end gap-x-4 gap-y-3 px-4 py-3">
      {children}
    </div>
  );
}
export function FilterField({ label, children, className = "" }: { label: string; children: ReactNode; className?: string }) {
  return (
    <label className={`flex min-w-36 flex-col gap-1 text-xs font-semibold text-ink-soft ${className}`}>
      {label}
      {children}
    </label>
  );
}

/** Titled block used on detail and overview pages. Pair with <Card> for the surface. */
export function Section({ id, title, aside, children }: { id: string; title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="mb-7">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 id={id} className="font-display text-xl font-bold tracking-tight">
          {title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function EmptyState({ title, children, action, icon = "info" }: { title: string; children?: ReactNode; action?: ReactNode; icon?: IconName }) {
  return (
    <div className="glass-strong flex gap-4 border-dashed px-6 py-8">
      <span className="grid size-10 shrink-0 place-items-center icon-tile">
        <Icon name={icon} />
      </span>
      <div className="min-w-0">
        <h2 className="font-display text-lg font-bold">{title}</h2>
        {children && <div className="mt-1.5 max-w-[65ch] text-ink-muted">{children}</div>}
        {action && <div className="mt-4">{action}</div>}
      </div>
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
    <div role="alert" className="flex gap-3 rounded-card border border-danger-line bg-danger-bg px-5 py-4 text-danger">
      <Icon name="warning" className="mt-0.5 size-5" />
      <div className="min-w-0">
        <p className="font-semibold">{title}</p>
        <p className="mt-0.5">{displayText(error.message)}</p>
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
    </div>
  );
}

/** The backend build does not expose this endpoint yet. Never shows sample figures. */
export function Unavailable({ endpoint, willShow }: { endpoint: string; willShow?: readonly ReactNode[] }) {
  return (
    <EmptyState title="Endpoint not yet available" icon="plug">
      <p>
        This page reads <code className="rounded-sm bg-canvas px-1 py-0.5 font-mono text-[0.9em]">{endpoint}</code>, which the backend does not expose in this
        build. No data is shown until it does.
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

// ---- data blocks --------------------------------------------------------------------------------------------------
/** Label/value pairs in a card. A null or empty value renders an em dash, never a blank. */
export function Facts({ items }: { items: readonly [string, ReactNode][] }) {
  return (
    <dl className="card divide-y divide-line px-5 py-1">
      {items.map(([k, v]) => (
        <div key={k} className="grid grid-cols-[minmax(6.5rem,35%)_1fr] gap-x-4 py-2.5">
          <dt className="text-ink-muted">{k}</dt>
          <dd className="min-w-0 font-medium wrap-break-word">{v === null || v === undefined || v === "" ? "None" : v}</dd>
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
      <div role="status" aria-label="Loading" className="card space-y-2.5 p-5">
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
    <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
      <p role="status" className="tnum text-ink-muted">
        {total !== undefined ? `${total.toLocaleString("en-US")} in total. ` : ""}Page {page.toLocaleString("en-US")} of {pages.toLocaleString("en-US")}
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
    <details className="card p-4">
      <summary className="cursor-pointer font-medium">{label}</summary>
      <pre className="mt-3 max-h-64 overflow-auto rounded-md border border-line bg-canvas p-3 font-mono text-xs">{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

export function Notice({ tone, children }: { tone: "ok" | "warn"; children: ReactNode }) {
  const c = tone === "ok" ? "border-ok-line bg-ok-bg text-ok" : "border-warn-line bg-warn-bg text-warn";
  return (
    <p role="status" className={`mb-3 flex items-center gap-2 rounded-control border px-4 py-2.5 ${c}`}>
      <Icon name={tone === "ok" ? "check-circle" : "warning"} className="size-4" />
      <span>{children}</span>
    </p>
  );
}
