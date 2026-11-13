"use client";

// Change against the previous period for one figure (command-center comparison block). When the earlier period is empty or
// unknown it says so instead of drawing an infinite percentage. Callers: /insights, /population.

import { Icon } from "@/components/ui";
import { fmtInt } from "@/lib/format";
import type { CcComparison } from "@/lib/types";

type Row = { current: number | null; previous: number | null; delta: number | null; delta_pp?: number | null; delta_pct?: number | null };

/** Looks the figure up in kpis, then flows, so callers pass the key only. */
export function CompareNote({ cmp, k, unit = "count" }: { cmp: CcComparison | undefined; k: string; unit?: "count" | "points" }) {
  if (!cmp) return null;
  const row: Row | undefined = cmp.kpis?.[k] ?? cmp.flows?.[k];
  if (!cmp.available || !row || row.previous == null || row.previous === 0 || row.delta == null) return <span className="text-xs text-ink-muted">No earlier period yet</span>;
  const up = row.delta > 0;
  const same = row.delta === 0;
  const pts = row.delta_pp ?? null;
  const text = unit === "points" && pts != null ? `${up ? "+" : ""}${pts.toFixed(1)} points` : `${up ? "+" : ""}${fmtInt(row.delta)}${row.delta_pct != null ? ` (${up ? "+" : ""}${row.delta_pct.toFixed(0)}%)` : ""}`;
  return (
    <span className={`inline-flex items-center gap-1 text-xs font-semibold ${same ? "text-ink-muted" : up ? "text-ok" : "text-danger"}`}>
      {!same && <Icon name={up ? "trend-up" : "trend-down"} className="size-3.5" />}
      {same ? "Same as the period before" : `${text} since the period before`}
    </span>
  );
}
