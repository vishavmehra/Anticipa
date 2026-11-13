"use client";

// Population funnel: residents, evaluated, due, case opened, planned, in outreach, closed. Reads GET /analytics/funnel
// (backend/anticipa/analytics/service.py `funnel`): every stage is an independent live count of distinct residents with
// its own definition. Bars share one baseline and one hue (dataviz: magnitude, ranked bars); the exact count is written
// at the end of each bar and a table view sits behind the chart. Callers: /monitor page; exported for the Command Center.
// Purpose: show judges how many residents reach each step.

import { api, isMissingEndpoint } from "@/lib/api";
import { fmtDateTime, fmtInt, humanize } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { Card, CardHeader, ChartTable, DataLabel, ErrorPanel, Unavailable, btnSecondary } from "@/components/ui";
import type { Funnel } from "./payloads";
import { fmtShare, plain } from "./shared";

const BAR = "#0d9488"; // design system chart teal (CHART.teal)

export function PopulationFunnel() {
  const res = useApi("funnel", (signal) => api<Funnel>("/analytics/funnel", { signal }));
  const f = res.data?.data;

  let body;
  if (res.error) {
    body = isMissingEndpoint(res.error) ? <Unavailable endpoint="GET /api/v1/analytics/funnel" /> : <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load the funnel" />;
  } else if (!f) {
    body = (
      <div role="status" aria-label="Loading the funnel" className="space-y-3 px-5 pb-5">
        {Array.from({ length: 7 }, (_, i) => (
          <div key={i} className="skeleton h-9 w-full" />
        ))}
      </div>
    );
  } else {
    const top = Math.max(1, ...f.stages.map((s) => s.count));
    body = (
      <div className="px-5 pb-5">
        <ol aria-label="Care gap funnel stages" className="space-y-4">
          {f.stages.map((s, i) => (
            <li key={s.key} data-stage={s.key}>
              <div className="flex items-center gap-3">
                <span className="w-28 shrink-0 font-medium text-ink-soft sm:w-40">{s.label}</span>
                <span className="h-6 min-w-10 flex-1 overflow-hidden rounded-r-[4px] bg-neutral-bg" aria-hidden="true">
                  <span className="block h-full rounded-r-[4px]" style={{ width: `${Math.max(0.6, (s.count / top) * 100)}%`, background: BAR }} />
                </span>
                <span className="tnum w-20 shrink-0 text-right text-base font-semibold">{fmtInt(s.count)}</span>
              </div>
              <p className="mt-1 pl-0 text-xs text-ink-muted sm:pl-[10.75rem]">
                {i === 0 ? "All residents loaded." : s.conversion_from_previous != null ? `${fmtShare(s.conversion_from_previous)} of the stage above.` : ""}
                {s.share_of_residents != null && i > 0 ? ` ${fmtShare(s.share_of_residents)} of all residents.` : ""}
                {s.cases != null ? ` ${fmtInt(s.cases)} cases.` : ""} {plain(s.definition)}
              </p>
            </li>
          ))}
        </ol>

        <div className="mt-5 rounded-control border border-line bg-canvas px-4 py-3">
          <p className="font-medium">Left the funnel without completion</p>
          <p className="text-xs text-ink-muted">{plain(f.leakage.definition)}</p>
          <ul className="mt-2 flex flex-wrap gap-x-6 gap-y-1">
            {f.leakage.items.map((l) => (
              <li key={l.status}>
                {humanize(l.status)}: <span className="tnum font-semibold">{fmtInt(l.cases)}</span>
              </li>
            ))}
          </ul>
        </div>
        <p className="mt-3 max-w-[80ch] text-xs text-ink-muted">{plain(f.note)}</p>
        <ChartTable
          caption="Care gap funnel"
          columns={["Stage", "Residents", "Of the stage above", "Of all residents"]}
          rows={f.stages.map((s) => [s.label, fmtInt(s.count), s.conversion_from_previous != null ? fmtShare(s.conversion_from_previous) : "None", fmtShare(s.share_of_residents)])}
        />
      </div>
    );
  }

  return (
    <Card as="section" aria-labelledby="funnel-title">
      <CardHeader
        id="funnel-title"
        icon="filter"
        title="Care gap funnel"
        subtitle={f ? `Counted ${fmtDateTime(f.generated_at)}. Refreshes at most every ${f.cache_ttl_seconds} seconds.` : "How many residents reach each step, from loaded to closed."}
        aside={
          <>
            <DataLabel kind="live" />
            <button type="button" className={btnSecondary} onClick={res.reload} disabled={res.loading}>
              Refresh
            </button>
          </>
        }
      />
      {body}
    </Card>
  );
}
