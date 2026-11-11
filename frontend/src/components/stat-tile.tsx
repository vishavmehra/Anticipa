import type { ReactNode } from "react";

/**
 * One measured value. `value === null` renders an explicit "Not available" so a missing
 * measurement can never be mistaken for zero. Per product spec §5.10 every metric states its basis.
 */
export function StatTile({
  label,
  value,
  basis,
  badge,
  loading = false,
}: {
  label: string;
  value: ReactNode | null;
  loading?: boolean;
  /** Numerator / denominator / period, or the data source. */
  basis?: ReactNode;
  badge?: ReactNode;
}) {
  return (
    <div className="rounded-md border border-line bg-surface px-4 py-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-ink-muted">{label}</p>
        {badge}
      </div>
      {loading ? (
        <div className="skeleton mt-2 mb-1 h-7 w-16" role="status" aria-label={`Loading ${label}`} />
      ) : (
        <p className={`tnum mt-1 font-semibold tracking-tight ${value === null ? "text-lg text-ink-muted" : "text-2xl"}`}>
          {value === null ? "Not available" : value}
        </p>
      )}
      {basis && <p className="mt-0.5 text-xs text-ink-muted">{basis}</p>}
    </div>
  );
}
