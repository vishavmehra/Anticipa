"use client";

// Impact and economics: what reading clinical notes adds over structured records alone, how many care gaps the optimised plan
// places within clinic capacity and how fairly, and what the AI reading costs. Every figure comes from GET /analytics/impact
// (a stored, read only baseline report) and shows its denominator and a "How this is measured" note. Nothing is projected and no
// money saving, staff time or health outcome is claimed because the data holds no defined estimate for them.
// Route: /evaluation, tab "Impact and economics". Purpose: show judges the measured difference Anticipa makes.

import { api } from "@/lib/api";
import { displayText } from "@/lib/display";
import { fmtDate, fmtInt, humanize } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { BarList, Card, CardHeader, DataLabel, EmptyState, ErrorPanel, Icon, KpiTile } from "@/components/ui";
import { GrowBar } from "./interactive";
import type { ImpactPlan, ImpactReport, ImpactTruth } from "./payloads";
import { fmtShare, fmtUsd } from "./shared";

const GUIDELINE_NAME: Record<string, string> = {
  brca_genetic_counseling_referral: "Genetic counselling referral",
  brca_risk_assessment: "BRCA risk assessment",
  breast_cancer_screening: "Breast cancer screening",
  cervical_cancer_screening: "Cervical cancer screening",
  colorectal_average_risk: "Colorectal screening, average risk",
  colorectal_family_history: "Colorectal screening, family history",
};
const SERVICE_NAME: Record<string, string> = {
  cervical_screening: "Cervical screening",
  colorectal_screening: "Colorectal screening",
  genetic_counselling: "Genetic counselling",
  mammography: "Mammography",
};
const name = (map: Record<string, string>, key: string) => map[key] ?? humanize(key).replace(/^./, (c) => c.toUpperCase());
const pts = (n: number | null | undefined) => (n == null ? "Not available" : `${n < 1 ? n.toFixed(2) : n.toFixed(1)} points`);
const pct1 = (n: number | null | undefined) => (n == null ? "Not available" : `${(n * 100).toFixed(1)}%`);
const ratio = (a: number, b: number) => (b > 0 ? a / b : null);

/** Server prose in product wording: no dashes, generated rather than synthetic, modelled rather than simulated, no snake case names. */
const clean = (s: string) =>
  displayText(s)
    .replace(/\bsynthetic\b/gi, "generated")
    .replace(/\bsimulated\b/gi, "modelled")
    .replace(/\b[a-z]+(?:_[a-z]+)+\b/g, (w) => w.replace(/_/g, " "));
/** Definitions worth showing next to the figures, in reading order. */
const DEFINITION_KEYS: [string, string][] = [
  ["case_opened_only_because_of_notes", "Care gap found only because notes were read"],
  ["found_as_due", "Flagged"],
  ["missed_as_not_due", "Missed"],
  ["duplicate_outreach_avoided", "Repeat invitation avoided"],
  ["paused_for_missing_information", "Held for review"],
  ["allocation_rate", "Share placed"],
  ["equity gap_points", "Access gap"],
  ["hard_constraint_violations", "Rule breaks"],
];

/** Collapsible method note. Native details keeps it keyboard and screen reader friendly with no script. */
function Method({ children }: { children: React.ReactNode }) {
  return (
    <details className="group mt-4 text-[0.8125rem]">
      <summary className="inline-flex cursor-pointer items-center gap-1.5 rounded-sm font-semibold text-brand-ink">
        <Icon name="info" className="size-4" />
        How this is measured
      </summary>
      <div className="mt-2 space-y-2 border-l-2 border-brand-light pl-3 text-ink-soft">{children}</div>
    </details>
  );
}

/** Two bars to compare one measure under two methods, each with its own count and denominator. */
function Versus({ label, a, b, aName, bName, lowerIsBetter = false }: { label: string; a: { n: number; d: number }; b: { n: number; d: number }; aName: string; bName: string; lowerIsBetter?: boolean }) {
  const ra = ratio(a.n, a.d) ?? 0;
  const rb = ratio(b.n, b.d) ?? 0;
  const top = Math.max(ra, rb, 0.0001);
  const better = lowerIsBetter ? (rb < ra ? "b" : "a") : rb > ra ? "b" : "a";
  const row = (who: "a" | "b", nm: string, v: { n: number; d: number }, r: number) => (
    <li className="grid gap-1 sm:grid-cols-[8.5rem_1fr_auto] sm:items-center sm:gap-3">
      <span className={`text-[0.8125rem] ${better === who ? "font-semibold text-ink" : "text-ink-muted"}`}>{nm}</span>
      <span className="block h-3 overflow-hidden rounded-full bg-brand-soft/70" aria-hidden="true">
        <GrowBar pct={Math.max(r > 0 ? 1.5 : 0, (r / top) * 100)} className={who === "b" ? "bg-linear-to-r from-brand to-[#9b83e6]" : "bg-[#bba6f1]"} />
      </span>
      <span className="tnum text-right text-[0.8125rem]">
        <span className="font-display text-base font-bold">{fmtShare(r)}</span> <span className="text-ink-muted">{fmtInt(v.n)} of {fmtInt(v.d)}</span>
      </span>
    </li>
  );
  return (
    <div>
      <h3 className="mb-2 font-semibold">{label}</h3>
      <ul className="space-y-2">
        {row("a", aName, a, ra)}
        {row("b", bName, b, rb)}
      </ul>
    </div>
  );
}

const planRow = (label: string, fcfs: string, opt: string, win: "fcfs" | "opt" | "tie") => ({ label, fcfs, opt, win });

function PlanTable({ f, o, d }: { f: ImpactPlan; o: ImpactPlan; d: NonNullable<ImpactReport["planner_baseline"]>["difference"] }) {
  const rows = [
    planRow("Care gaps placed", fmtInt(f.allocated), fmtInt(o.allocated), f.allocated === o.allocated ? "tie" : o.allocated > f.allocated ? "opt" : "fcfs"),
    planRow("Share of care gaps placed", fmtShare(f.allocation_rate.value), fmtShare(o.allocation_rate.value), "tie"),
    planRow("District access gap", pts(d.district_equity_gap_points.first_come), pts(d.district_equity_gap_points.ortools), d.district_equity_gap_points.ortools < d.district_equity_gap_points.first_come ? "opt" : "fcfs"),
    planRow("Language access gap", pts(d.language_equity_gap_points.first_come), pts(d.language_equity_gap_points.ortools), d.language_equity_gap_points.ortools < d.language_equity_gap_points.first_come ? "opt" : "fcfs"),
    planRow("Placed in own district", pct1(d.own_district_share.first_come), pct1(d.own_district_share.ortools), d.own_district_share.ortools > d.own_district_share.first_come ? "opt" : "fcfs"),
    planRow("Median wait from plan start", `${d.median_wait_days.first_come.toFixed(1)} days`, `${d.median_wait_days.ortools.toFixed(1)} days`, "tie"),
    planRow("Rule breaks", fmtInt(d.hard_constraint_violations.first_come), fmtInt(d.hard_constraint_violations.ortools), "tie"),
  ];
  return (
    <div className="overflow-x-auto" role="region" aria-label="Plan comparison table" tabIndex={0}>
      <table className="w-full min-w-[30rem] border-collapse text-left">
        <caption className="sr-only">First come first served compared with the optimised plan</caption>
        <thead>
          <tr className="border-b border-line text-ink-muted">
            <th scope="col" className="py-2 font-medium">Measure</th>
            <th scope="col" className="py-2 text-right font-medium">First come first served</th>
            <th scope="col" className="py-2 text-right font-medium">Optimised plan</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label} className="border-b border-line last:border-0">
              <th scope="row" className="py-2 font-medium">{r.label}</th>
              <td className={`tnum py-2 text-right ${r.win === "fcfs" ? "font-bold" : ""}`}>{r.fcfs}</td>
              <td className={`tnum py-2 text-right ${r.win === "opt" ? "rounded-md bg-brand-soft/70 font-bold text-brand-ink" : ""}`}>{r.opt}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function side(t: ImpactTruth, k: "rules_only" | "with_notes") {
  return { due: { n: t[k].truly_due.found_as_due, d: t[k].truly_due.pairs }, call: { n: t[k].truly_not_due.unnecessary_outreach, d: t[k].truly_not_due.pairs } };
}

export function ProvenImpact() {
  const res = useApi("impact-report", (signal) => api<ImpactReport>("/analytics/impact", { signal }));
  const r = res.data?.data;
  if (res.error) {
    return res.error.status === 404 ? (
      <EmptyState title="No impact report has been produced yet" icon="gauge">The measured baselines appear here once the impact report has been run.</EmptyState>
    ) : (
      <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load the impact figures" />
    );
  }
  if (!r) return <div className="skeleton h-96 w-full" role="status" aria-label="Loading impact figures" />;

  const m = r.measured_inputs;
  const rb = r.rules_baseline;
  const up = rb.uplift_from_reading_notes;
  const all = up.all;
  const planner = r.planner_baseline;
  const f = planner.first_come_first_served;
  const o = planner.ortools_cpsat;
  const cohort = rb.scope.residents;
  const found = all.residents_with_case_opened_only_because_of_notes ?? all.case_opened_only_because_of_notes;
  const cost = m.ai_extraction_cost;
  const perResident = ratio(cost.total_cost_usd, m.ai_cohort.residents);
  const perFound = ratio(cost.total_cost_usd, found);
  const evidence = { a: side(rb.against_planted_evidence_truth, "rules_only"), b: side(rb.against_planted_evidence_truth, "with_notes") };
  const screening = { a: side(rb.against_planted_screening_truth, "rules_only"), b: side(rb.against_planted_screening_truth, "with_notes") };
  const bySource = Object.entries(up)
    .filter(([k, v]) => k !== "all" && v.case_opened_only_because_of_notes > 0)
    .sort((x, y) => y[1].case_opened_only_because_of_notes - x[1].case_opened_only_because_of_notes);
  const topSource = bySource[0]?.[1].case_opened_only_because_of_notes ?? 1;
  const held = all.changes.paused_for_missing_information ?? 0;
  const services = Object.entries(o.coverage_by_service).sort((x, y) => y[1].coverage - x[1].coverage);
  const sameTotal = f.allocated === o.allocated;
  const d = planner.difference;
  const limits = (r.limitations ?? []).filter((l) => !/pending clinical review/i.test(l)).map(clean);
  const defs = DEFINITION_KEYS.flatMap(([k, label]) => (r.definitions?.[k] ? [[label, clean(r.definitions[k])] as [string, string]] : []));

  return (
    <div className="space-y-5">
      <p className="flex flex-wrap items-center gap-3 text-ink-soft">
        <DataLabel kind="evaluation" />
        Measured on {fmtDate(r.generated_at)}. Every figure shows what it was counted from. Nothing here is projected.
      </p>

      <div role="region" aria-label="Impact figures" className="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-4 [&>*]:min-w-0">
        <KpiTile icon="eye" label="Residents found that records alone miss" value={fmtInt(found)} basis={`of ${fmtInt(cohort)} residents whose notes were read (${fmtShare(ratio(found, cohort))})`} />
        <KpiTile icon="calendar-check" label="Care gaps placed within capacity" value={fmtInt(o.allocated)} basis={`of ${fmtInt(o.cases)} care gaps (${fmtShare(o.allocation_rate.value)}) using ${fmtInt(o.seats_total)} seats`} />
        <KpiTile icon="scale" label="District access gap" value={pts(d.district_equity_gap_points.ortools)} basis={`down from ${pts(d.district_equity_gap_points.first_come)} when seats go first come first served`} />
        <KpiTile icon="cpu" label="AI cost per resident read" value={fmtUsd(perResident, 4)} basis={`${fmtUsd(cost.total_cost_usd)} over ${fmtInt(m.ai_cohort.residents)} residents, ${fmtInt(cost.documents)} documents`} />
      </div>

      <Card as="section" aria-labelledby="pi-notes">
        <CardHeader id="pi-notes" icon="eye" title="Reading notes finds care gaps that records alone miss" subtitle="The same approved rules, once on structured records only and once on records plus the notes" aside={<DataLabel kind="evaluation" />} />
        <div className="grid gap-8 px-5 pb-5 xl:grid-cols-2 [&>*]:min-w-0">
          <div className="space-y-6">
            <Versus label="Residents who should be flagged, and were" a={evidence.a.due} b={evidence.b.due} aName="Records alone" bName="Records and notes" />
            <Versus label="Residents invited who should not have been" a={screening.a.call} b={screening.b.call} aName="Records alone" bName="Records and notes" lowerIsBetter />
            <p className="text-[0.8125rem] text-ink-muted">
              The trade is caution: {fmtInt(held)} screening decisions that records alone would invite are held for review because the notes are undated or unclear.
            </p>
          </div>
          <div>
            <h3 className="mb-2 font-semibold">Care gaps that exist only because notes were read</h3>
            <BarList
              ariaLabel="Care gaps opened only because notes were read, by guideline"
              tableCaption="Care gaps opened only because notes were read, by guideline"
              valueHeader="Care gaps"
              labelWidth="w-32 sm:w-44"
              max={topSource}
              rows={bySource.map(([k, v]) => ({ label: name(GUIDELINE_NAME, k), value: v.case_opened_only_because_of_notes, display: fmtInt(v.case_opened_only_because_of_notes) }))}
            />
            <p className="mt-3 text-[0.8125rem] text-ink-muted">
              {fmtInt(all.case_opened_only_because_of_notes)} care gap decisions across {fmtInt(all.pairs)} resident and guideline pairs. Records alone found none of the family history cases.
            </p>
          </div>
        </div>
        <div className="px-5 pb-5">
          <Method>
            <p>
              For each of the {fmtInt(cohort)} residents whose notes were read, the approved guideline engine runs twice: with structured records only, then with records and the facts read from notes. The engine is deterministic, so any difference comes from the notes.
            </p>
            <p>
              Flagged and invited counts are scored against reference labels for {fmtInt(rb.scope.ground_truth.dev_residents)} development residents: {fmtInt(rb.against_planted_evidence_truth.pairs)} resident and guideline pairs for the first measure and {fmtInt(rb.against_planted_screening_truth.pairs)} screening pairs for the second. The held back test group was not used.
            </p>
          </Method>
        </div>
      </Card>

      <Card as="section" aria-labelledby="pi-plan">
        <CardHeader id="pi-plan" icon="scale" title="Care gaps placed within capacity, and who gets the seats" subtitle="The optimised plan against a first come first served plan on the same care gaps and seats" aside={<DataLabel kind="evaluation" />} />
        <div className="grid gap-8 px-5 pb-5 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] [&>*]:min-w-0">
          <div>
            <PlanTable f={f} o={o} d={d} />
            <p className="mt-3 text-[0.8125rem] text-ink-muted">
              {sameTotal
                ? `Both plans place ${fmtInt(o.allocated)} of ${fmtInt(o.cases)} care gaps because seats run out. The optimiser changes who gets a seat, not how many.`
                : `The optimised plan places ${fmtInt(o.allocated - f.allocated)} more care gaps than first come first served.`}
            </p>
          </div>
          <div>
            <h3 className="mb-2 font-semibold">Share of demand placed, by service</h3>
            <BarList
              ariaLabel="Share of demand placed by service"
              tableCaption="Share of demand placed by service in the optimised plan"
              valueHeader="Placed"
              labelWidth="w-28 sm:w-36"
              max={1}
              rows={services.map(([k, v]) => ({ label: name(SERVICE_NAME, k), value: v.coverage, display: fmtShare(v.coverage), note: `${fmtInt(v.allocated)} of ${fmtInt(v.demand)}` }))}
            />
          </div>
        </div>
        <div className="px-5 pb-5">
          <Method>
            <p>
              Both plans use exactly the {fmtInt(planner.inputs.cases)} care gaps and {fmtInt(planner.inputs.usable_seats)} usable seats of the latest optimised planning run. First come first served takes care gaps in the order they were opened and gives each the nearest clinic with a free seat.
            </p>
            <p>
              The access gap is the difference in the share placed between the best served and the worst served district (and language group), in percentage points. A smaller gap is fairer. Seats are modelled clinic capacity, so counts show how the plan behaves rather than real clinic availability.
            </p>
            <p>Every care gap currently carries the same priority, so coverage of high priority cases is not measured here.</p>
          </Method>
        </div>
      </Card>

      <div className="grid items-start gap-5 xl:grid-cols-2 [&>*]:min-w-0">
        <Card as="section" aria-labelledby="pi-cost">
          <CardHeader id="pi-cost" icon="cpu" title="What reading the notes costs" subtitle="AI spend for the residents whose notes were read" aside={<DataLabel kind="evaluation" />} />
          <dl className="divide-y divide-line px-5 pb-2">
            {[
              ["Per resident read", fmtUsd(perResident, 4), `${fmtUsd(cost.total_cost_usd)} over ${fmtInt(m.ai_cohort.residents)} residents`],
              ["Per resident found that records miss", fmtUsd(perFound, 4), `${fmtUsd(cost.total_cost_usd)} over ${fmtInt(found)} residents`],
              ["Per 1,000 documents", fmtUsd(cost.usd_per_1000_documents, 2), `${fmtInt(cost.documents)} documents, ${fmtInt(cost.calls)} model calls`],
              ["Spent on second reads of hard documents", fmtUsd(cost.escalation_cost_usd, 2), `${fmtShare(ratio(cost.escalation_cost_usd, cost.total_cost_usd))} of the total`],
            ].map(([label, v, sub]) => (
              <div key={label} className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 py-3">
                <dt className="col-start-1 font-medium text-ink-soft">{label}</dt>
                <dd className="col-start-1 text-xs text-ink-muted">{sub}</dd>
                <dd className="tnum col-start-2 row-span-2 row-start-1 self-center font-display text-xl font-bold">{v}</dd>
              </div>
            ))}
          </dl>
          <div className="px-5 pb-5">
            <Method>
              <p>Total spend is the estimated model cost of reading every document of the {fmtInt(m.ai_cohort.residents)} residents, from the final evaluation run. Each figure divides that total by the count shown beneath it.</p>
              <p>The cost is estimated from configured prices and has not been reconciled with a provider bill.</p>
            </Method>
          </div>
        </Card>

        <Card as="section" aria-labelledby="pi-limits">
          <CardHeader id="pi-limits" icon="shield-alert" title="What these figures do not show" />
          <ul className="space-y-2.5 px-5 pb-5 text-ink-soft">
            <li>Staff hours saved are not estimated, because no defined time model is held.</li>
            {limits.map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        </Card>
      </div>

      {defs.length > 0 && (
        <Card as="section" aria-labelledby="pi-defs">
          <CardHeader id="pi-defs" icon="scroll" title="How each figure is defined" />
          <dl className="grid gap-x-8 gap-y-3 px-5 pb-5 lg:grid-cols-2 [&>*]:min-w-0">
            {defs.map(([label, text]) => (
              <div key={label}>
                <dt className="font-semibold">{label}</dt>
                <dd className="text-ink-muted">{text}</dd>
              </div>
            ))}
          </dl>
        </Card>
      )}
    </div>
  );
}
