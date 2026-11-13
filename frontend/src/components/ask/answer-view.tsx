"use client";

import { AreaTrend, BarList, Icon, Pill } from "@/components/ui";
import { argSummary, formatValue, modelLabel, plain, sourceLabel, toolLabel } from "./format";
import type { Turn } from "./use-ask";
import type { AskAnswer, AskChartData, AskTableData } from "./types";

function Chart({ chart }: { chart: AskChartData }) {
  const fmt = (n: number) => formatValue(n, chart.unit);
  const title = plain(chart.title);
  if (chart.kind === "line") {
    return <AreaTrend points={chart.data.map((d) => ({ label: d.x, value: d.y }))} yFormat={fmt} ariaLabel={title} tableCaption={title} seriesLabel={plain(chart.y_label)} height={180} />;
  }
  const top = Math.max(...chart.data.map((d) => d.y), Number.EPSILON); // bars are relative to the largest value (BarList defaults to a scale of at least 1, which would hide small rates)
  return <BarList rows={chart.data.map((d) => ({ label: plain(d.x), value: d.y, display: fmt(d.y) }))} max={top} ariaLabel={title} tableCaption={title} valueHeader={plain(chart.y_label)} labelWidth="w-28" />;
}

function DataTable({ table }: { table: AskTableData }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-[0.8125rem]">
        <caption className="sr-only">{plain(table.title)}</caption>
        <thead>
          <tr className="border-b border-line text-ink-muted">
            {table.columns.map((c, i) => (
              <th key={c.key} scope="col" className={`px-2 py-1.5 font-medium ${i ? "text-right" : ""}`}>
                {plain(c.label)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((r, ri) => (
            <tr key={ri} className="border-b border-line last:border-0">
              {table.columns.map((c, i) => (
                <td key={c.key} className={`tnum px-2 py-1.5 ${i ? "text-right" : ""}`}>
                  {formatValue(r[c.key], c.unit)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function HowIGotThis({ a }: { a: AskAnswer }) {
  if (!a.sources.length) return null;
  return (
    <details className="mt-3 rounded-control border border-line bg-[var(--control-bg)] px-3 py-2 text-[0.8125rem]">
      <summary className="cursor-pointer font-medium text-brand-ink">How I got this</summary>
      <ol className="mt-2 space-y-3">
        {a.sources.map((s) => (
          <li key={s.call_id}>
            <p className="flex flex-wrap items-center gap-2 font-semibold text-ink">
              {toolLabel(s.tool)}
              <Pill tone="neutral">{sourceLabel(s.source)}</Pill>
            </p>
            <p className="mt-0.5 text-ink-soft">{plain(s.definition)}</p>
            <p className="mt-0.5 text-ink-muted">Filters used: {argSummary(s.args)}</p>
          </li>
        ))}
      </ol>
      <p className="mt-3 border-t border-line pt-2 text-ink-muted">
        {a.cached ? "Served from a recent identical answer. " : ""}
        Checked in {a.iterations || 1} {a.iterations === 1 ? "step" : "steps"} in {(a.latency_ms / 1000).toFixed(1)} seconds
        {a.model ? ` by ${modelLabel(a.model)}` : ""}. Every number was matched against the data before it was shown. {plain(a.synthetic_banner)}
      </p>
    </details>
  );
}

function Skeleton() {
  return (
    <div role="status" aria-live="polite" className="glass-card rounded-card px-4 py-3">
      <p className="flex items-center gap-2 text-[0.8125rem] font-medium text-brand-ink">
        <Icon name="refresh" className="size-4 animate-spin motion-reduce:animate-none" />
        Looking at the data
      </p>
      <div className="mt-3 space-y-2" aria-hidden="true">
        <div className="skeleton h-3.5 w-11/12 rounded" />
        <div className="skeleton h-3.5 w-9/12 rounded" />
        <div className="skeleton h-3.5 w-6/12 rounded" />
      </div>
    </div>
  );
}

/** One question and its answer, in every state: loading, error, refused, unverified and answered. */
export function TurnView({ turn, onFollowUp, onRetry }: { turn: Turn; onFollowUp: (q: string) => void; onRetry: (id: number) => void }) {
  const a = turn.answer;
  return (
    <article aria-label={`Question: ${turn.question}`} className="space-y-3">
      <p className="ml-auto w-fit max-w-[90%] rounded-card rounded-br-sm bg-brand-soft px-4 py-2.5 text-[0.9375rem] text-brand-ink">{turn.question}</p>

      {turn.status === "loading" && <Skeleton />}

      {turn.status === "error" && turn.error && (
        <div role="alert" className="rounded-card border border-danger-line bg-danger-bg px-4 py-3 text-[0.875rem] text-danger">
          <p className="flex items-start gap-2 font-medium">
            <Icon name="alert" className="mt-0.5 size-4" />
            {turn.error.message}
          </p>
          {turn.error.code !== "FORBIDDEN" && turn.error.code !== "UNAUTHENTICATED" && (
            <button type="button" onClick={() => onRetry(turn.id)} className="mt-2 rounded-control border border-danger-line bg-[var(--control-bg)] px-3 py-1.5 text-[0.8125rem] font-semibold">
              Try again
            </button>
          )}
        </div>
      )}

      {turn.status === "done" && a && (
        <div className="glass-card rounded-card px-4 py-3.5">
          {a.refused ? (
            <div className="flex items-start gap-2.5 text-[0.9375rem]">
              <Icon name="info" className="mt-0.5 size-[1.1rem] text-brand-ink" />
              <div>
                <p className="font-semibold text-ink">This is outside what Ask Anticipa can answer</p>
                <p className="mt-1 text-ink-soft">{plain(a.answer)}</p>
              </div>
            </div>
          ) : !a.grounded ? (
            <div className="rounded-control border border-warn-line bg-warn-bg px-3 py-2.5 text-[0.875rem] text-warn">
              <p className="flex items-start gap-2 font-semibold">
                <Icon name="warning" className="mt-0.5 size-4" />
                Could not verify this answer
              </p>
              <p className="mt-1">{plain(a.notice ?? "Some figures could not be checked against the data.")} The charts and tables below come straight from the data.</p>
              <details className="mt-2">
                <summary className="cursor-pointer font-medium underline underline-offset-2">Show the unverified text</summary>
                <p className="mt-1.5 text-ink-soft">{plain(a.answer)}</p>
              </details>
            </div>
          ) : (
            <p className="text-[0.9375rem] leading-6 whitespace-pre-line text-ink">{plain(a.answer)}</p>
          )}

          {a.chart && (
            <figure className="tile mt-3 rounded-control p-3">
              <figcaption className="mb-2 text-[0.8125rem] font-semibold text-ink">{plain(a.chart.title)}</figcaption>
              <Chart chart={a.chart} />
            </figure>
          )}
          {a.table && (
            <div className="tile mt-3 rounded-control p-3">
              <p className="mb-1 text-[0.8125rem] font-semibold text-ink">{plain(a.table.title)}</p>
              <DataTable table={a.table} />
            </div>
          )}
          {!a.refused && <HowIGotThis a={a} />}

          {a.follow_ups.length > 0 && (
            <div className="mt-3">
              <p className="mb-1.5 text-[0.75rem] font-semibold tracking-wide text-ink-muted uppercase">You could also ask</p>
              <ul className="flex flex-wrap gap-2">
                {a.follow_ups.map((q) => (
                  <li key={q}>
                    <button type="button" onClick={() => onFollowUp(plain(q))} className="rounded-full border border-line-strong/40 bg-[var(--control-bg)] px-3 py-1.5 text-left text-[0.8125rem] font-medium text-brand-ink hover:bg-brand-soft">
                      {plain(q)}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </article>
  );
}
