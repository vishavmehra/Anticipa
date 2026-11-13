"use client";

// Evaluation dashboard: how well the pipeline performs against known answers, from the latest stored run
// (GET /analytics/evaluation, /runs, /runs/{id}; backend/anticipa/evaluation). Every figure is a measured value of a real
// run, with its sample size and interval; a metric that could not be measured says so and is never shown as zero.
// Honest empty state until a run exists. Route: /evaluation. Purpose: show judges the measured quality and its limits.

import { useState } from "react";
import { api, apiList, isMissingEndpoint } from "@/lib/api";
import { LANGUAGES, fmtDateTime, fmtInt, humanize } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { DataTable, type Column } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { BarList, Card, CardHeader, DataLabel, EmptyState, ErrorPanel, Facts, KpiTile, PageHeader, Pill, btnSecondary, selectClass, type BarRow } from "@/components/ui";
import type { EvaluationRun, EvaluationRunSummary, Gate, Rate } from "./evaluation-types";
import { PopulationFunnel } from "./funnel";
import { fmtUsd, plain } from "./shared";

const LANG_ORDER = ["en", "hi", "ar"] as const;

/** One decimal percent: 97.4%. null stays "Not measured": a missing measurement is never a zero. */
const pc = (v: number | null | undefined) => (v == null ? "Not measured" : `${(Math.round(v * 1000) / 10).toFixed(1)}%`);
const interval = (r?: Rate) => (r?.ci95 ? `${pc(r.ci95[0])} to ${pc(r.ci95[1])}` : "No interval");
const sampleNote = (r?: Rate) => (r?.n ? `n ${fmtInt(r.n)}${r.flags?.includes("low_n") ? ", few samples" : ""}` : "");
const shortNote = (r?: Rate) => (r?.n ? `n ${fmtInt(r.n)}` : "");
/** The word "synthetic" never appears in the interface (owner rule); stored limitation text says "generated" instead. */
const wording = (s: string) => plain(s).replace(/synthetic/gi, "generated").replace(/^./, (c) => c.toUpperCase());

const LANG_METRICS = [
  { id: "EX-01.A", label: "Precision", help: "Of the facts accepted, the share that match a known fact." },
  { id: "EX-02.A", label: "Recall", help: "Of the known facts, the share found and accepted." },
  { id: "EX-03.A", label: "F1 score", help: "One score that combines precision and recall." },
  { id: "EX-06", label: "Quotes verified", help: "Share of candidate facts whose exact quote was found in the note." },
  { id: "EX-08a", label: "Subject accuracy", help: "Share of matched facts attributed to the right person." },
  { id: "EX-09", label: "Negation and certainty accuracy", help: "Share of matched facts with the right status: stated, negated or uncertain." },
  { id: "EX-12", label: "Hallucination rate", help: "Share of accepted facts that match no known fact. Lower is better." },
] as const;

const TIER_LABEL: Record<string, string> = {
  synthetic_population: "Residents loaded",
  ai_evidence_cohort: "Residents with notes read by AI",
  intensive_evaluation: "Residents scored in this run",
};

function BreakdownChart({ metrics, dim, order, nameOf }: { metrics: Record<string, Rate>; dim: "language" | "category"; order: readonly string[]; nameOf: (k: string) => string }) {
  const [sel, setSel] = useState<string>(LANG_METRICS[0].id);
  const m = LANG_METRICS.find((x) => x.id === sel)!;
  const cells = metrics[sel]?.breakdowns?.[dim] ?? {};
  const keys = order.length ? order : Object.keys(cells).sort();
  const rows: BarRow[] = keys.filter((k) => cells[k]?.value != null).map((k) => ({ label: nameOf(k), value: (cells[k].value ?? 0) * 100, display: pc(cells[k].value), note: shortNote(cells[k]) }));
  const few = keys.filter((k) => cells[k]?.flags?.includes("low_n") && cells[k]?.value != null);
  const missing = keys.filter((k) => cells[k]?.value == null);
  const id = `metric-${dim}`;
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <label htmlFor={id} className="font-medium">
          Metric
        </label>
        <select id={id} value={sel} onChange={(e) => setSel(e.target.value)} className={`${selectClass} max-w-xs`}>
          {LANG_METRICS.map((x) => (
            <option key={x.id} value={x.id}>
              {x.label}
            </option>
          ))}
        </select>
      </div>
      <p className="mb-3 text-ink-muted">{m.help}</p>
      {rows.length === 0 ? (
        <p className="text-ink-muted">This metric was not measured for any {dim}.</p>
      ) : (
        <BarList rows={rows} max={100} ariaLabel={`${m.label} by ${dim}`} tableCaption={`${m.label} by ${dim}`} valueHeader={m.label} labelWidth="w-32 sm:w-44" />
      )}
      {few.length > 0 && <p className="mt-2 text-xs text-ink-muted">Few samples, read with care: {few.map(nameOf).join(", ")}.</p>}
      {missing.length > 0 && <p className="mt-2 text-xs text-ink-muted">Not measured: {missing.map(nameOf).join(", ")}.</p>}
    </div>
  );
}

function LanguageTable({ metrics }: { metrics: Record<string, Rate> }) {
  const cols: Column<(typeof LANG_METRICS)[number]>[] = [
    { key: "m", header: "Metric", cell: (m) => <span title={m.help}>{m.label}</span> },
    ...LANG_ORDER.map((l) => ({
      key: l,
      header: LANGUAGES[l],
      align: "right" as const,
      cell: (m: (typeof LANG_METRICS)[number]) => {
        const r = metrics[m.id]?.breakdowns?.language?.[l];
        return r ? (
          <span>
            <span className="font-semibold">{pc(r.value)}</span>
            <span className="block text-xs text-ink-muted">
              {interval(r)}
              {sampleNote(r) ? `, ${sampleNote(r)}` : ""}
            </span>
          </span>
        ) : (
          <span className="text-ink-muted">Not measured</span>
        );
      },
    })),
    { key: "all", header: "All languages", align: "right", cell: (m) => <span className="font-semibold">{pc(metrics[m.id]?.value)}</span> },
  ];
  return <DataTable caption="Metrics by language" columns={cols} rows={LANG_METRICS} getRowId={(m) => m.id} loading={false} error={null} />;
}

const th = (v: number | boolean | null | undefined) => (v == null ? "Not set" : typeof v === "boolean" ? (v ? "Yes" : "No") : v > 0 && v <= 1 ? `${Math.round(v * 1000) / 10}%` : String(v));

function GateTable({ gates }: { gates: Gate[] }) {
  const [filter, setFilter] = useState("all");
  const rows = gates.filter((g) => filter === "all" || (filter === "open" ? ["not_evaluated", "skipped"].includes(g.result) : g.result === filter));
  const cols: Column<Gate>[] = [
    {
      key: "g",
      header: "Check",
      cell: (g) => (
        <>
          <span className="font-medium">{g.description ? plain(g.description) : g.metric_id}</span>
          <span className="block font-mono text-xs text-ink-muted">
            {g.gate_id} on {g.metric_id}
          </span>
        </>
      ),
    },
    { key: "t", header: "Required", cell: (g) => `${g.comparator} ${th(g.threshold)}${g.threshold_per_language != null ? `, ${th(g.threshold_per_language)} per language` : ""}${g.threshold_status === "proposal" ? " (proposed)" : ""}` },
    {
      key: "o",
      header: "Measured",
      align: "right",
      cell: (g) => (
        <>
          <span className="font-semibold">{g.observed == null ? "Not measured" : th(g.observed)}</span>
          {g.observed_by_language && Object.values(g.observed_by_language).some((v) => v != null) && (
            <span className="block text-xs text-ink-muted">{LANG_ORDER.map((l) => `${l.toUpperCase()} ${th(g.observed_by_language?.[l])}`).join(", ")}</span>
          )}
        </>
      ),
    },
    { key: "r", header: "Result", cell: (g) => <StatusBadge status={g.result} /> },
  ];
  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <label htmlFor="gate-filter" className="font-medium">
          Show
        </label>
        <select id="gate-filter" value={filter} onChange={(e) => setFilter(e.target.value)} className={`${selectClass} max-w-xs`}>
          <option value="all">All checks</option>
          <option value="pass">Passed</option>
          <option value="fail">Failed</option>
          <option value="warn">Warnings</option>
          <option value="open">Not measured or skipped</option>
        </select>
      </div>
      <DataTable caption="Quality checks" columns={cols} rows={rows} getRowId={(g) => g.gate_id} loading={false} error={null} emptyTitle="No checks match this filter" />
    </>
  );
}

function Dashboard({ r }: { r: EvaluationRun }) {
  const M = r.metrics ?? {};
  const g = r.gate_summary ?? {};
  const evaluated = (g.pass ?? 0) + (g.fail ?? 0) + (g.warn ?? 0);
  const pf1 = M["PF-01"];
  const lat = M["PF-04"]?.overall;
  const saf = Object.entries(M).filter(([k]) => k.startsWith("SAF-")).sort(([a], [b]) => a.localeCompare(b));
  const categories = Object.keys(M["EX-02.A"]?.breakdowns?.category ?? {}).sort();
  const spend = Object.entries(pf1?.by_provider_model_task ?? {});
  const safCols: Column<[string, Rate]>[] = [
    { key: "c", header: "Check", cell: ([id, m]) => <>{plain(m.metric ?? id)}<span className="block font-mono text-xs text-ink-muted">{id}</span></> },
    { key: "n", header: "Errors", align: "right", cell: ([, m]) => (m.count != null ? fmtInt(m.count) : "Not measured") },
    { key: "d", header: "Out of", align: "right", cell: ([, m]) => (m.denominator != null ? fmtInt(m.denominator) : "Not measured") },
    { key: "v", header: "Rate", align: "right", cell: ([, m]) => <span className="font-semibold">{pc(m.value)}</span> },
  ];
  const spendCols: Column<[string, NonNullable<Rate["by_provider_model_task"]>[string]]>[] = [
    { key: "k", header: "Model and task", cell: ([k]) => <span className="font-mono text-xs break-all">{k}</span> },
    { key: "c", header: "Calls", align: "right", cell: ([, v]) => fmtInt(v.calls) },
    { key: "t", header: "Tokens in and out", align: "right", cell: ([, v]) => `${fmtInt(v.input_tokens)} and ${fmtInt(v.output_tokens)}` },
    { key: "u", header: "Estimated cost", align: "right", cell: ([, v]) => fmtUsd(v.cost_usd, 4) },
  ];

  return (
    <div className="space-y-6">
      <Card as="section" aria-labelledby="run-title">
        <CardHeader id="run-title" icon="flask" title="This run" subtitle="A run compares what the pipeline found with known answers. It checks the software. It is not clinical validation." aside={<DataLabel kind="measured">Evaluation</DataLabel>} />
        <div className="px-5 pb-5">
          <Facts
            items={[
              ["Run", <span key="r" className="font-mono text-xs break-all">{r.run_id}</span>],
              ["Status", <StatusBadge key="s" status={r.status} />],
              ["Type", humanize(r.run_type)],
              ["Dataset", <span key="d" className="font-mono text-xs">{r.dataset_version}</span>],
              ["Answers checked", r.split === "test" ? "Held out test answers" : r.split === "dev" ? "Development answers" : humanize(r.split)],
              ["Started", fmtDateTime(r.started_at)],
              ["Finished", r.completed_at ? fmtDateTime(r.completed_at) : "Not finished"],
              ["Code version", r.code?.git_commit ? `${r.code.git_commit.slice(0, 10)}${r.code.dirty ? ", with uncommitted changes" : ""}` : "Not recorded"],
              [
                "Models",
                r.model_versions?.length ? (
                  <span key="models" className="space-y-0.5">
                    {r.model_versions.map((m) => (
                      <span key={`${m.model_id}-${m.prompt_version}`} className="block">
                        {m.provider} <span className="font-mono text-xs">{m.model_id}</span>
                        {m.prompt_version ? (
                          <>
                            , prompt <span className="font-mono text-xs">{m.prompt_version}</span>
                          </>
                        ) : null}
                      </span>
                    ))}
                  </span>
                ) : (
                  "Not recorded"
                ),
              ],
            ]}
          />
        </div>
      </Card>

      <section aria-label="Headline results" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <KpiTile icon="check-circle" tint={(g.fail ?? 0) > 0 ? "rose" : "green"} label="Checks passed" value={evaluated ? `${g.pass ?? 0} of ${evaluated}` : null} basis={`${g.not_evaluated ?? 0} not measured, ${g.skipped ?? 0} skipped.`} />
        <KpiTile icon="shield" tint="teal" label="Quotes verified" value={M["EX-06"]?.value == null ? null : pc(M["EX-06"].value)} basis={M["EX-06"]?.denominator != null ? `${fmtInt(M["EX-06"].numerator)} of ${fmtInt(M["EX-06"].denominator)} candidate facts.` : "Not measured in this run."} />
        <KpiTile icon="alert" tint="amber" label="Hallucination rate" value={M["EX-12"]?.value == null ? null : pc(M["EX-12"].value)} basis={M["EX-12"]?.denominator != null ? `${fmtInt(M["EX-12"].numerator)} of ${fmtInt(M["EX-12"].denominator)} accepted facts. Lower is better.` : "Not measured in this run."} />
        <KpiTile icon="database" tint="blue" label="Cost per 1,000 notes" value={pf1?.value == null ? null : fmtUsd(pf1.value)} basis={pf1?.total_cost_usd != null ? `${fmtUsd(pf1.total_cost_usd, 4)} over ${fmtInt(pf1.calls)} model calls. Estimated from configured prices.` : "Not measured in this run."} />
        <KpiTile icon="clock" tint="violet" label="Median model latency" value={lat?.p50_ms == null ? null : `${Math.round(lat.p50_ms)} ms`} basis={lat ? `95th percentile ${lat.p95_ms == null ? "withheld, under 20 calls" : `${Math.round(lat.p95_ms)} ms`}. ${fmtInt(lat.n)} calls.` : "Not measured in this run."} />
      </section>

      <Card as="section" aria-labelledby="by-lang">
        <CardHeader id="by-lang" icon="languages" title="Results by language" subtitle="English, Hindi and Arabic are measured separately. Cells with few samples are flagged." aside={<DataLabel kind="measured">Evaluation</DataLabel>} />
        <div className="space-y-5 px-5 pb-5">
          <BreakdownChart metrics={M} dim="language" order={LANG_ORDER} nameOf={(k) => LANGUAGES[k] ?? k} />
          <LanguageTable metrics={M} />
        </div>
      </Card>

      <Card as="section" aria-labelledby="by-cat">
        <CardHeader id="by-cat" icon="layers" title="Results by category" subtitle="The kind of fact: family history, screening, conditions and so on." aside={<DataLabel kind="measured">Evaluation</DataLabel>} />
        <div className="px-5 pb-5">{categories.length ? <BreakdownChart metrics={M} dim="category" order={categories} nameOf={humanize} /> : <p className="text-ink-muted">No category breakdown was recorded for this run.</p>}</div>
      </Card>

      <Card as="section" aria-labelledby="safety">
        <CardHeader id="safety" icon="shield" title="Safety checks" subtitle="Mistakes that would matter most, counted among accepted facts. Lower is better. Zero is required for facts without a verified quote." aside={<DataLabel kind="measured">Evaluation</DataLabel>} />
        <div className="px-5 pb-5">
          <DataTable caption="Safety checks" columns={safCols} rows={saf} getRowId={([id]) => id} loading={false} error={null} emptyTitle="No safety checks were recorded" />
          {M["EX-13"]?.status === "not_evaluated" || M["EX-13"]?.reason ? <p className="mt-2 text-xs text-ink-muted">Adjudicated hallucination rate: not measured. {plain(M["EX-13"]?.reason)}</p> : null}
        </div>
      </Card>

      <Card as="section" aria-labelledby="gates">
        <CardHeader id="gates" icon="checklist" title="Quality checks" subtitle="Each check compares a measured value with a required value. A check that could not be measured is shown as not measured. It is never counted as passed." aside={<DataLabel kind="measured">Evaluation</DataLabel>} />
        <div className="px-5 pb-5">
          <GateTable gates={r.gates ?? []} />
        </div>
      </Card>

      <Card as="section" aria-labelledby="cost">
        <CardHeader id="cost" icon="cpu" title="Cost and speed" subtitle={pf1?.cost_basis?.startsWith("estimated_from_config") ? "Cost is estimated from configured prices. It is not reconciled with a provider bill." : pf1?.cost_basis ? `Cost basis: ${plain(pf1.cost_basis)}.` : "Cost is estimated from configured prices."} aside={<Pill tone="warn">Estimated</Pill>} />
        <div className="px-5 pb-5">
          <DataTable caption="Cost by model and task" columns={spendCols} rows={spend} getRowId={([k]) => k} loading={false} error={null} emptyTitle="No model calls were recorded for these notes" />
        </div>
      </Card>

      {(r.coverage?.length ?? 0) > 0 && (
        <Card as="section" aria-labelledby="coverage">
          <CardHeader id="coverage" icon="gauge" title="Coverage" subtitle="How much of each target group this run actually covered." />
          <div className="px-5 pb-5">
            <BarList
              rows={r.coverage!.map((c) => ({ label: TIER_LABEL[c.tier] ?? humanize(c.tier), value: c.target > 0 ? (c.actual_completed / c.target) * 100 : 0, display: `${fmtInt(c.actual_completed)} of ${fmtInt(c.target)}` }))}
              max={100}
              ariaLabel="Coverage against target"
              tableCaption="Coverage against target"
              valueHeader="Completed of target"
              labelWidth="w-36 sm:w-56"
            />
          </div>
        </Card>
      )}

      <Card as="section" aria-labelledby="limits">
        <CardHeader id="limits" icon="info" title="Limits of this evaluation" subtitle="Read these before quoting any number." />
        <div className="space-y-4 px-5 pb-5">
          {(r.limitations?.length ?? 0) > 0 ? (
            <ol className="max-w-[80ch] list-decimal space-y-1.5 pl-5">
              {r.limitations!.map((l, i) => (
                <li key={i}>{wording(l)}</li>
              ))}
            </ol>
          ) : (
            <p className="text-ink-muted">This run lists no limitations. A full report always does, so treat it as incomplete.</p>
          )}
          {(r.exclusions?.length ?? 0) > 0 && (
            <div>
              <h3 className="mb-1 font-semibold">Left out of the scores</h3>
              <ul className="list-disc space-y-1 pl-5">
                {r.exclusions!.map((x, i) => (
                  <li key={i}>
                    {wording(x.item)}: {wording(x.reason)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {r.failures && (
            <p>
              Notes that failed after retries: <span className="font-semibold">{fmtInt(r.failures.documents_failed ?? 0)}</span>
              {Object.keys(r.failures.by_error_category ?? {}).length > 0 ? `. By cause: ${Object.entries(r.failures.by_error_category ?? {}).map(([k, v]) => `${humanize(k)} ${v}`).join(", ")}.` : "."}
            </p>
          )}
        </div>
      </Card>
    </div>
  );
}

/** `embedded` drops the page title and funnel so the dashboard can sit in a tab of the Programs and impact page. */
export function EvaluationDashboard({ embedded = false }: { embedded?: boolean }) {
  const [picked, setPicked] = useState<string | null>(null);
  const runs = useApi("eval-runs", (s) => apiList<EvaluationRunSummary>("/analytics/evaluation/runs", { page_size: 20 }, s));
  const res = useApi(`eval:${picked ?? "latest"}`, (s) => api<EvaluationRun>(picked ? `/analytics/evaluation/runs/${picked}` : "/analytics/evaluation", { signal: s }));
  const list = runs.data?.data ?? [];

  const refresh = () => {
    res.reload();
    runs.reload();
  };
  return (
    <>
      {embedded ? (
        <div className="mb-4 flex flex-wrap items-center justify-end gap-2">
          <>
            {list.length > 1 && (
              <>
                <label htmlFor="eval-run" className="font-medium">
                  Run
                </label>
                <select id="eval-run" value={picked ?? list[0].run_id} onChange={(e) => setPicked(e.target.value === list[0].run_id ? null : e.target.value)} className={`${selectClass} max-w-xs`}>
                  {list.map((x) => (
                    <option key={x.run_id} value={x.run_id}>
                      {fmtDateTime(x.started_at)}, {humanize(x.run_type)}
                    </option>
                  ))}
                </select>
              </>
            )}
            <button type="button" className={btnSecondary} onClick={refresh} disabled={res.loading}>
              Refresh
            </button>
          </>
        </div>
      ) : (
        <>
          <PageHeader
            title="Evaluation"
            description="How well the pipeline performs against known answers. Every figure comes from a stored run, with its sample size."
            actions={
          <>
            {list.length > 1 && (
              <>
                <label htmlFor="eval-run" className="font-medium">
                  Run
                </label>
                <select id="eval-run" value={picked ?? list[0].run_id} onChange={(e) => setPicked(e.target.value === list[0].run_id ? null : e.target.value)} className={`${selectClass} max-w-xs`}>
                  {list.map((x) => (
                    <option key={x.run_id} value={x.run_id}>
                      {fmtDateTime(x.started_at)}, {humanize(x.run_type)}
                    </option>
                  ))}
                </select>
              </>
            )}
            <button type="button" className={btnSecondary} onClick={refresh} disabled={res.loading}>
              Refresh
            </button>
          </>
            }
          />
          <div className="mb-6">
            <PopulationFunnel />
          </div>
        </>
      )}
      {res.error ? (
        isMissingEndpoint(res.error) ? (
          <EmptyState title="No evaluation run yet" icon="flask">
            An evaluation run compares the pipeline with known answers. The first finished run appears here. Until then nothing is shown, and no target is presented as a result.
          </EmptyState>
        ) : (
          <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load the evaluation" />
        )
      ) : !res.data ? (
        <p role="status" className="text-ink-muted">
          Loading the latest run.
        </p>
      ) : (
        <Dashboard r={res.data.data} />
      )}
    </>
  );
}
