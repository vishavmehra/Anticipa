"use client";

// Age and sex pyramid from GET /analytics/population-explorer (pyramid[{age_band, sex, residents, open_care_gaps,
// due_residents}]): males to the left, females to the right, oldest band on top. Selecting a band row filters the explorer to
// that age group, so the chart cross filters with the cards beside it. Bars grow from the centre with a transform, and a
// table view sits behind the chart. Callers: /insights, /population. Purpose: show who the population is.

import { ageBandLabel, fmtInt } from "@/lib/format";
import { ChartTable } from "@/components/ui";
import { GrowBar } from "./interactive";
import type { PyramidCell } from "./payloads";
import { fmtShare } from "./shared";

export function Pyramid({ cells, bands, band, onBand, measure = "residents" }: { cells: PyramidCell[]; bands: string[]; band: string; onBand: (b: string) => void; measure?: "residents" | "open_care_gaps" | "due_residents" }) {
  const of = (b: string, sex: string) => cells.filter((c) => c.age_band === b && c.sex === sex).reduce((s, c) => s + c[measure], 0);
  const order = [...bands, ...[...new Set(cells.map((c) => c.age_band))].filter((b) => !bands.includes(b))];
  const max = Math.max(1, ...order.flatMap((b) => [of(b, "male"), of(b, "female")]));
  const male = cells.filter((c) => c.sex === "male").reduce((s, c) => s + c[measure], 0);
  const female = cells.filter((c) => c.sex === "female").reduce((s, c) => s + c[measure], 0);
  const all = cells.reduce((s, c) => s + c[measure], 0);
  const other = all - male - female;
  const rows = [...order].reverse();
  return (
    <div>
      <ul aria-label="Population pyramid by age band and sex" className="space-y-1">
        {rows.map((b) => {
          const m = of(b, "male");
          const f = of(b, "female");
          return (
            <li key={b} className="grid grid-cols-[1fr_6.75rem_1fr] items-center gap-2">
              <span className="flex items-center justify-end gap-2">
                <span className="tnum text-xs text-ink-muted">{fmtInt(m)}</span>
                <span className="block h-3.5 w-full max-w-80 scale-x-[-1] overflow-hidden rounded-full bg-brand-soft/70" aria-hidden="true">
                  <GrowBar pct={(m / max) * 100} />
                </span>
              </span>
              <button type="button" aria-pressed={band === b} onClick={() => onBand(band === b ? "" : b)} className={`min-h-7 whitespace-nowrap rounded-full px-2 text-center text-[0.8125rem] font-semibold ${band === b ? "bg-brand text-white" : "text-ink-soft hover:bg-brand-soft"}`}>
                {ageBandLabel(b)}
              </button>
              <span className="flex items-center gap-2">
                <span className="block h-3.5 w-full max-w-80 overflow-hidden rounded-full bg-brand-soft/70" aria-hidden="true">
                  <GrowBar pct={(f / max) * 100} className="bg-[#bba6f1]" />
                </span>
                <span className="tnum text-xs text-ink-muted">{fmtInt(f)}</span>
              </span>
            </li>
          );
        })}
      </ul>
      <ul aria-label="Pyramid legend" className="mt-3 flex flex-wrap gap-x-5 gap-y-1">
        <li className="flex items-center gap-1.5"><span aria-hidden="true" className="size-3 rounded-full bg-brand" />Male <span className="tnum font-semibold">{fmtShare(all ? male / all : null)}</span></li>
        <li className="flex items-center gap-1.5"><span aria-hidden="true" className="size-3 rounded-full bg-[#bba6f1]" />Female <span className="tnum font-semibold">{fmtShare(all ? female / all : null)}</span></li>
        {other > 0 && <li className="text-ink-muted">Other or not stated {fmtInt(other)}</li>}
      </ul>
      <ChartTable caption="Population by age band and sex" columns={["Age band", "Male", "Female"]} rows={[...order].reverse().map((b) => [ageBandLabel(b), fmtInt(of(b, "male")), fmtInt(of(b, "female"))])} />
    </div>
  );
}
