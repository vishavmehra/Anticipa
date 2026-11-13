"use client";

// Care planning studio: the four planning steps with their live state, a plan summary from real counts, three planning
// scenarios that map to real planner settings (priority weights, equity weight) and the Generate plan action that submits
// POST /planning/runs for the coming week. Observers see everything and a disabled button with the reason. Real data only:
// GET /care-gaps counts, GET /guidelines, GET /analytics/capacity, GET /planning/runs. Route: /planning (Plan tab).
// Purpose: show judges a plan being set up against real demand and capacity.

import { useState } from "react";
import { api, apiList, apiPost, type Envelope } from "@/lib/api";
import { fmtDateTime, fmtInt, humanize } from "@/lib/format";
import { PERM } from "@/lib/nav";
import type { CareGap, Guideline, PlanningRun, RunMetrics } from "@/lib/types";
import { useAction } from "@/lib/use-action";
import { useApi } from "@/lib/use-api";
import { GatedButton } from "@/components/gated-button";
import { StatusBadge } from "@/components/status-badge";
import { Card, CardHeader, ErrorPanel, Notice, Pill, btnPrimary, type IconName, GlassTile, type DuoName, Icon } from "@/components/ui";
import { GrowBar, InfoTip } from "./interactive";
import { useCapacity } from "./kit";
import { fmtShare } from "./shared";

type Scenario = "population" | "high_risk" | "equity";
const SCENARIOS: { key: Scenario; title: string; blurb: string; icon: DuoName; settings: Record<string, unknown> }[] = [
  { key: "population", title: "Population wide", blurb: "Maximise preventive coverage with the planner's default weights.", icon: "population", settings: {} },
  { key: "high_risk", title: "High risk first", blurb: "Weight urgent and high priority cases far above the rest.", icon: "shield", settings: { priority_weights: { urgent: 20, high: 12, medium: 2, routine: 1, low: 1 } } },
  { key: "equity", title: "Equity focus", blurb: "Add an equity weight across districts and languages.", icon: "decide", settings: { equity_constraints: { equity_weight: 0.5, dimensions: ["district", "language"] } } },
];

async function countWaiting(signal: AbortSignal): Promise<number> {
  const r: Envelope<CareGap[]> = await api<CareGap[]>("/care-gaps?page_size=1&status=approved_for_planning&status=awaiting_capacity", { signal });
  return r.meta.total ?? 0;
}

const STEPS = [
  { title: "Select population", blurb: "Cases waiting for a slot" },
  { title: "Define interventions", blurb: "Screenings with an approved guideline" },
  { title: "Check capacity", blurb: "Clinics and open seats this week" },
  { title: "Generate plan", blurb: "Optimised, fair and actionable" },
] as const;

function Layers() {
  const layers: { label: string; icon: IconName }[] = [
    { label: "Population", icon: "users" },
    { label: "Risk and eligibility", icon: "shield" },
    { label: "Capacity", icon: "calendar" },
  ];
  return (
    <div aria-hidden="true" className="relative mx-auto h-72 w-full max-w-md [perspective:1000px]">
      {layers.map((l, i) => (
        <div
          key={l.label}
          className="absolute top-2 left-1/2 flex h-44 w-72 items-end rounded-[2rem] border border-white/80 bg-linear-to-br from-white/90 via-brand-soft to-white/70 p-4 shadow-pop"
          style={{ transform: `translate(-50%, ${i * 62}px) rotateX(60deg) rotateZ(-34deg)`, zIndex: 3 - i }}
        >
          <span className="flex items-center gap-2 font-semibold text-brand-ink">
            <Icon name={l.icon} className="size-5" />
            {l.label}
          </span>
        </div>
      ))}
    </div>
  );
}


function Delta({ now, before, pct }: { now: number | null | undefined; before: number | null | undefined; pct?: boolean }) {
  if (now == null || before == null) return <span className="text-xs text-ink-muted">No earlier run to compare</span>;
  const d = now - before;
  if (d === 0) return <span className="text-xs text-ink-muted">Same as the run before</span>;
  const txt = pct ? `${d > 0 ? "+" : ""}${(d * 100).toFixed(1)} points` : `${d > 0 ? "+" : ""}${fmtInt(d)}`;
  return (
    <span className={`inline-flex items-center gap-1 text-xs font-semibold ${d > 0 ? "text-ok" : "text-danger"}`}>
      <Icon name={d > 0 ? "arrow-up-right" : "arrow-right"} className={`size-3.5 ${d > 0 ? "" : "rotate-45"}`} />
      {txt} since the run before
    </span>
  );
}

function LatestRun({ last, lm, prev, pm, onOpenAllocations, loading }: { last: PlanningRun | undefined; lm: RunMetrics; prev: PlanningRun | undefined; pm: RunMetrics; onOpenAllocations?: () => void; loading: boolean }) {
  if (loading) return <div className="skeleton h-40" role="status" aria-label="Loading the latest run" />;
  if (!last) {
    return (
      <Card as="section" aria-labelledby="lr-h" className="glass-strong">
        <CardHeader id="lr-h" icon="clock" title="Latest run" />
        <p className="px-5 pb-5 text-ink-muted">No plan has been generated yet. Choose a scenario and select Generate plan. The result, what changed and why cases were left out appear here.</p>
      </Card>
    );
  }
  const reasons = Object.entries(lm.reason_counts ?? {}).sort(([, a], [, b]) => b - a);
  const top = Math.max(1, ...reasons.map(([, n]) => n));
  const items = [
    { k: "Cases in the run", v: fmtInt(lm.cases_total), d: <Delta now={lm.cases_total} before={pm.cases_total} /> },
    { k: "Allocated a slot", v: fmtInt(lm.allocated), d: <Delta now={lm.allocated} before={pm.allocated} /> },
    { k: "Coverage", v: lm.coverage == null ? "Not available" : fmtShare(lm.coverage), d: <Delta now={lm.coverage} before={pm.coverage} pct /> },
  ];
  return (
    <Card as="section" aria-labelledby="lr-h" className="glass-strong">
      <CardHeader id="lr-h" icon="clock" title="Latest run" subtitle={`Created ${fmtDateTime(last.created_at)}${prev ? `. Compared with the run before, ${fmtDateTime(prev.created_at)}.` : "."}`} aside={<StatusBadge status={last.status} />} />
      <div className="grid gap-6 px-5 pb-5 lg:grid-cols-2 *:min-w-0">
        <div>
          <h3 className="mb-2 font-semibold">What changed</h3>
          <ul aria-label="What changed since the run before" className="space-y-3">
            {items.map((i) => (
              <li key={i.k} className="flex items-center justify-between gap-3 rounded-control border border-line px-4 py-2.5">
                <div>
                  <p className="text-ink-soft">{i.k}</p>
                  <p>{i.d}</p>
                </div>
                <span className="tnum text-xl font-bold">{i.v}</span>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h3 className="mb-2 font-semibold">
            Why cases were left out <InfoTip text="Each case the planner could not place has a reason code and an explanation. Open the allocations to read them." />
          </h3>
          {reasons.length === 0 ? (
            <p className="text-ink-muted">Every case in the run was allocated, or the run recorded no reasons.</p>
          ) : (
            <ul aria-label="Reasons cases were left out" className="space-y-2.5">
              {reasons.map(([reason, n]) => (
                <li key={reason} className="flex items-center gap-3">
                  <span className="w-40 shrink-0 text-ink-soft">{humanize(reason)}</span>
                  <span className="h-2.5 min-w-8 flex-1 overflow-hidden rounded-full bg-brand-soft" aria-hidden="true">
                    <GrowBar pct={(n / top) * 100} />
                  </span>
                  <span className="tnum w-16 shrink-0 text-right font-semibold">{fmtInt(n)}</span>
                </li>
              ))}
            </ul>
          )}
          {onOpenAllocations && (
            <button type="button" onClick={onOpenAllocations} className="mt-3 inline-flex items-center gap-1 text-brand-ink underline underline-offset-2">
              See the allocations and unallocated cases <Icon name="arrow-right" className="size-4" />
            </button>
          )}
        </div>
      </div>
    </Card>
  );
}

export function PlanningStudio({ onGenerated, onOpenAllocations }: { onGenerated?: () => void; onOpenAllocations?: () => void }) {
  const [scenario, setScenario] = useState<Scenario>("population");
  const waiting = useApi("studio-waiting", (s) => countWaiting(s));
  const guidelines = useApi("studio-guidelines", (s) => apiList<Guideline>("/guidelines", { page_size: 50 }, s));
  const cap = useCapacity();
  const latest = useApi("studio-latest-run", (s) => apiList<PlanningRun>("/planning/runs", { page_size: 2 }, s));
  const act = useAction();
  const [queued, setQueued] = useState(false);

  const activities = new Set((guidelines.data?.data ?? []).flatMap((g) => (g.versions ?? []).filter((v) => v.review_status === "approved").map((v) => v.care_activity)));
  const seats = cap.data?.data.capacity.remaining_seats ?? null;
  const need = waiting.data ?? null;
  const last = latest.data?.data[0];
  const prev = latest.data?.data[1];
  const lm = (last?.metrics ?? {}) as RunMetrics;
  const pm = (prev?.metrics ?? {}) as RunMetrics;

  const done = [need != null, guidelines.data != null && activities.size > 0, seats != null, false];
  const current = done.findIndex((d) => !d);
  const state = (i: number) => (done[i] ? "done" : i === (current === -1 ? 3 : current) ? "current" : "todo");

  async function generate() {
    const start = new Date();
    const end = new Date(start.getTime() + 7 * 24 * 3600 * 1000);
    const body = { planning_period: { start: start.toISOString(), end: end.toISOString() }, ...SCENARIOS.find((s) => s.key === scenario)!.settings };
    setQueued(false);
    if (await act.run(() => apiPost("/planning/runs", body))) {
      setQueued(true);
      latest.reload();
      onGenerated?.();
    }
  }

  return (
    <div className="space-y-6">
      <div className="grid gap-6 xl:grid-cols-[17rem_minmax(0,1fr)_21rem] *:min-w-0">
        <ol aria-label="Planning steps" className="space-y-1 self-center">
          {STEPS.map((s, i) => {
            const st = state(i);
            return (
              <li key={s.title} data-step={i + 1} data-state={st} className={`flex items-start gap-3 rounded-card px-3 py-3 ${st === "current" ? "bg-brand-soft" : ""}`}>
                <span aria-hidden="true" className={`mt-0.5 grid size-8 shrink-0 place-items-center rounded-full border-2 text-sm font-bold ${st === "done" ? "border-brand bg-brand text-white" : st === "current" ? "border-brand bg-surface text-brand-ink" : "border-line-strong bg-surface text-ink-muted"}`}>
                  {st === "done" ? <Icon name="check" className="size-4" /> : i + 1}
                </span>
                <span>
                  <span className="block font-semibold">
                    {s.title}
                    <span className="sr-only">, {st === "done" ? "complete" : st === "current" ? "current step" : "not started"}</span>
                  </span>
                  <span className="block text-[0.8125rem] text-ink-muted">
                    {i === 0 && need != null ? `${fmtInt(need)} cases waiting for a slot` : i === 1 && guidelines.data ? `${activities.size} screenings with an approved guideline` : i === 2 && seats != null ? `${fmtInt(seats)} seats open this week` : s.blurb}
                  </span>
                </span>
              </li>
            );
          })}
        </ol>

        <Layers />

        <Card as="section" aria-labelledby="ps-h" className="glass-strong">
          <CardHeader id="ps-h" icon="target" title="Plan summary" aside={last ? <StatusBadge status={last.status} /> : <Pill tone="neutral">No run yet</Pill>} />
          <dl className="space-y-3 px-5 pb-5">
            {[
              { icon: "users" as IconName, k: "Cases waiting for a slot", v: need == null ? "Not available" : fmtInt(need) },
              { icon: "layers" as IconName, k: "Interventions in scope", v: guidelines.data ? fmtInt(activities.size) : "Not available" },
              { icon: "calendar" as IconName, k: "Seats needed", v: need == null ? "Not available" : fmtInt(need) },
            ].map((r) => (
              <div key={r.k} className="flex items-center gap-3">
                <Icon name={r.icon} className="size-5 text-brand-ink" />
                <dt className="flex-1 text-ink-soft">{r.k}</dt>
                <dd className="tnum text-lg font-bold">{r.v}</dd>
              </div>
            ))}
            <div className="flex items-center gap-3">
              <Icon name="building" className="size-5 text-brand-ink" />
              <dt className="flex-1 text-ink-soft">Open seats this week</dt>
              <dd className="tnum flex items-center gap-1.5 text-lg font-bold">
                {seats == null ? "Not available" : fmtInt(seats)}
                {seats != null && need != null && <Icon name={seats >= need ? "check-circle" : "warning"} className={`size-5 ${seats >= need ? "text-ok" : "text-warn"}`} />}
              </dd>
            </div>
          </dl>
          <div className="space-y-1 px-5 pb-3">
            {seats != null && need != null && seats < need && <p className="text-[0.8125rem] text-warn">Short by {fmtInt(need - seats)} seats. The planner will leave the rest unallocated and say why.</p>}
            {last && (
              <p className="text-[0.8125rem] text-ink-muted">
                Latest run {fmtDateTime(last.created_at)}: {fmtInt(lm.allocated)} of {fmtInt(lm.cases_total)} cases allocated.
              </p>
            )}
          </div>
          <div className="px-5 pb-5">
            <GatedButton permission={PERM.capacityManage} className={`${btnPrimary} min-h-12 w-full text-base`} disabled={act.pending} onClick={generate}>
              {act.pending ? "Submitting the plan" : "Generate plan"}
              <Icon name="arrow-right" className="size-5" />
            </GatedButton>
            {act.error && <div className="mt-3"><ErrorPanel error={act.error} title="The plan was not submitted" /></div>}
            {queued && <div className="mt-3"><Notice tone="ok">The plan was submitted for the next 7 days. It appears under Track when the job finishes.</Notice></div>}
          </div>
        </Card>
      </div>

      <LatestRun last={last} lm={lm} prev={prev} pm={pm} onOpenAllocations={onOpenAllocations} loading={latest.loading && !latest.data} />

      <section aria-labelledby="sc-h">
        <div className="mb-3 flex items-center gap-3">
          <Icon name="chart" className="size-6 text-brand-ink" />
          <div>
            <h2 id="sc-h" className="font-serif text-xl font-semibold">Planning scenarios</h2>
            <p className="text-ink-muted">Compare different strategies and choose the best plan.</p>
          </div>
        </div>
        <div role="radiogroup" aria-label="Planning scenario" className="grid gap-4 lg:grid-cols-3">
          {SCENARIOS.map((s) => {
            const on = scenario === s.key;
            return (
              <button
                key={s.key}
                type="button"
                role="radio"
                aria-checked={on}
                onClick={() => setScenario(s.key)}
                className={`glass-strong flex items-center gap-4 rounded-card border p-4 text-left transition-colors ${on ? "border-brand ring-2 ring-brand/30" : "border-line hover:bg-canvas"}`}
              >
                <span aria-hidden="true" className={`grid size-5 shrink-0 place-items-center rounded-full border-2 ${on ? "border-brand" : "border-line-strong"}`}>
                  {on && <span className="size-2.5 rounded-full bg-brand" />}
                </span>
                <GlassTile name={s.icon} size="md" />
                <span>
                  <span className="block font-semibold">{s.title}</span>
                  <span className="block text-[0.8125rem] text-ink-muted">{s.blurb}</span>
                </span>
              </button>
            );
          })}
        </div>
        <p className="mt-2 text-xs text-ink-muted">Each scenario sets planner options only. Priority weights and the equity weight are the planner&apos;s own settings, and every run keeps its settings for review. Scenario {humanize(scenario)} is selected.</p>
      </section>
    </div>
  );
}
