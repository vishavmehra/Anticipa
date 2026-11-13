"use client";

// Live processing monitor: the 1M pipeline (residents loaded, batches, storage profiles, throughput, ETA), AI extraction
// jobs, AI spend against the approved budget and document counts. Polls GET /analytics/processing every 10 seconds
// (backend/anticipa/analytics/service.py `processing`, cached server side for 10 s). Every value is a live count or a
// rate measured from recent batches; an unknown rate shows "Not available", never a guess. Route: /monitor.
// Purpose: show judges the pipeline working in real time.

import { useEffect, useState } from "react";
import { isMissingEndpoint } from "@/lib/api";
import { api } from "@/lib/api";
import { fmtDateTime, fmtInt, humanize } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { useCan } from "@/components/app-shell";
import { StatusBadge } from "@/components/status-badge";
import { Card, CardHeader, DataLabel, EmptyState, ErrorPanel, KpiTile, Pill, ProgressBar, Unavailable, btnSecondary } from "@/components/ui";
import type { PipelineAvailable, Processing, ProcessingJob } from "./payloads";
import { fmtDuration, fmtShare, fmtUsd, plain } from "./shared";

const POLL_MS = 10_000;

const jobName = (t: string) => humanize(t.replace(/\./g, " "));
const hoursToText = (h: number | null | undefined) => (h == null ? "Not available" : h === 0 ? "Done" : fmtDuration(h * 3600));
const perHour = (v: number | null | undefined) => (v == null ? "Not available" : `${fmtInt(v)} per hour`);

function Pipeline({ p }: { p: Processing["pipeline"] }) {
  if (!p.available) {
    return (
      <Card as="section" aria-labelledby="pipe">
        <CardHeader id="pipe" icon="database" title="Resident pipeline" aside={<DataLabel kind="live" />} />
        <p className="px-5 pb-5 text-ink-muted">{plain(p.message)}</p>
      </Card>
    );
  }
  const a: PipelineAvailable = p;
  const state = a.state === "complete" ? <Pill tone="ok" icon="check-circle">Complete</Pill> : a.state === "running" ? <Pill tone="info" icon="activity">Running</Pill> : <Pill tone="neutral" icon="clock">Idle</Pill>;
  const full = a.ingestion.profiles.full;
  const rules = a.ingestion.profiles.rules;
  return (
    <Card as="section" aria-labelledby="pipe">
      <CardHeader
        id="pipe"
        icon="database"
        title="Resident pipeline"
        subtitle={<>Dataset <span className="font-mono text-xs">{a.dataset_version}</span>. Target {fmtInt(a.resident_target)} residents in batches of {fmtInt(a.batch_size)}.</>}
        aside={
          <>
            {state}
            <DataLabel kind="live" />
          </>
        }
      />
      <div className="space-y-5 px-5 pb-5">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <KpiTile icon="users" tint="teal" label="Residents loaded" value={fmtInt(a.ingestion.residents_loaded)} basis={`${fmtShare(a.ingestion.percent)} of ${fmtInt(a.resident_target)}.`} />
          <KpiTile icon="layers" tint="blue" label="Batches loaded" value={`${fmtInt(a.ingestion.batches_loaded)} of ${fmtInt(a.generation.batches_planned)}`} basis={`${fmtInt(a.generation.batches_completed)} generated, ${fmtInt(a.generation.batches_running)} running, ${fmtInt(a.generation.batches_failed)} failed.`} />
          <KpiTile icon="zap" tint="amber" label="Loading speed" value={perHour(a.ingestion.throughput_residents_per_hour)} basis={a.ingestion.throughput_basis ? plain(a.ingestion.throughput_basis) : undefined} />
          <KpiTile icon="hourglass" tint="violet" label="Time left" value={hoursToText(a.eta_hours)} basis={plain(a.eta_basis)} />
        </div>

        <div className="grid gap-5 lg:grid-cols-2 *:min-w-0">
          <div>
            <h3 className="mb-2 font-semibold">Progress</h3>
            <div className="space-y-3">
              <div>
                <p className="mb-1 text-ink-muted">Loaded into the database</p>
                <ProgressBar done={a.ingestion.residents_loaded} total={a.resident_target} label="Residents loaded" tone="brand" />
              </div>
              <div>
                <p className="mb-1 text-ink-muted">Generated on disk</p>
                <ProgressBar done={a.generation.residents_generated} total={a.resident_target} label="Residents generated" tone="info" />
                <p className="mt-1 text-xs text-ink-muted">
                  {fmtInt(a.generation.residents_generated)} residents generated. Speed: {perHour(a.generation.throughput_residents_per_hour).toLowerCase()}. Time left: {hoursToText(a.generation.eta_hours).toLowerCase()}.
                </p>
              </div>
            </div>
          </div>
          <div>
            <h3 className="mb-2 font-semibold">Storage profile of loaded residents</h3>
            <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 rounded-control border border-line bg-canvas px-4 py-3">
              <dt className="text-ink-muted">Full detail</dt>
              <dd className="tnum font-semibold">{fmtInt(full)}</dd>
              <dt className="text-ink-muted">Rules only</dt>
              <dd className="tnum font-semibold">{fmtInt(rules)}</dd>
            </dl>
            <p className="mt-2 text-xs text-ink-muted">{a.ingestion.profiles_basis ? plain(a.ingestion.profiles_basis) : "Full keeps all clinical detail. Rules only keeps what the guideline rules need."}</p>
            <p className="mt-2 text-xs text-ink-muted">
              Last batch written {a.manifest_updated_at ? fmtDateTime(a.manifest_updated_at) : "Not recorded"}
              {a.idle_seconds != null ? `. Quiet for ${fmtDuration(a.idle_seconds)}.` : "."}
            </p>
          </div>
        </div>
      </div>
    </Card>
  );
}

function JobRow({ j }: { j: ProcessingJob }) {
  const stale = j.status === "running" && j.heartbeat_age_seconds != null && j.heartbeat_age_seconds > 120;
  return (
    <li className="rounded-control border border-line px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{jobName(j.job_type)}</span>
        <StatusBadge status={j.status} />
        <span className="font-mono text-xs text-ink-muted">{j.id.slice(0, 8)}</span>
        {stale && <Pill tone="warn" icon="warning">No heartbeat for {fmtDuration(j.heartbeat_age_seconds)}</Pill>}
        <span className="ml-auto text-xs text-ink-muted">Updated {fmtDateTime(j.updated_at)}</span>
      </div>
      <div className="mt-2">
        <ProgressBar done={j.processed_items} total={j.total_items} label={`${jobName(j.job_type)} progress`} tone={j.status === "failed" ? "danger" : j.status === "completed" ? "ok" : "brand"} />
        <p className="mt-1 text-xs text-ink-muted">
          {fmtInt(j.processed_items)} of {j.total_items == null ? "an unknown number of" : fmtInt(j.total_items)} items. {fmtInt(j.succeeded_items)} succeeded, {fmtInt(j.failed_items)} failed.
        </p>
      </div>
      {j.error_summary && <p className="mt-1 text-xs text-danger">{plain(j.error_summary)}</p>}
    </li>
  );
}

function Jobs({ jobs }: { jobs: Processing["jobs"] }) {
  const statuses = Object.entries(jobs.by_status).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Card as="section" aria-labelledby="jobs-h">
      <CardHeader id="jobs-h" icon="layers" title="Background jobs" subtitle="AI extraction, guideline evaluation, planning and the other long running jobs." aside={<DataLabel kind="live" />} />
      <div className="space-y-4 px-5 pb-5">
        <ul aria-label="Jobs by status" className="flex flex-wrap gap-x-5 gap-y-1">
          {statuses.length === 0 ? <li className="text-ink-muted">No jobs have run yet.</li> : statuses.map(([s, n]) => <li key={s}><StatusBadge status={s} /> <span className="tnum font-semibold">{fmtInt(n)}</span></li>)}
          <li>
            Failed in the last 24 hours <span className="tnum font-semibold">{fmtInt(jobs.failed_last_24h)}</span>
          </li>
        </ul>
        <div>
          <h3 className="mb-2 font-semibold">Running now</h3>
          {jobs.active.length === 0 ? <p className="text-ink-muted">No job is running or waiting.</p> : <ul aria-label="Active jobs" className="space-y-2">{jobs.active.map((j) => <JobRow key={j.id} j={j} />)}</ul>}
        </div>
        <div>
          <h3 className="mb-2 font-semibold">Most recent</h3>
          {jobs.recent.length === 0 ? <p className="text-ink-muted">Nothing to show yet.</p> : <ul aria-label="Recent jobs" className="space-y-2">{jobs.recent.map((j) => <JobRow key={j.id} j={j} />)}</ul>}
        </div>
      </div>
    </Card>
  );
}

function Spend({ s }: { s: Processing["ai_spend"] }) {
  const a = s.anthropic;
  const n = s.nebius;
  const cap = (v: number) => (v > 0 ? fmtUsd(v) : "No cap set");
  return (
    <Card as="section" aria-labelledby="spend">
      <CardHeader id="spend" icon="cpu" title="AI spend against the approved budget" subtitle="Cost recorded by the model router for every call. Cached answers cost nothing." aside={<DataLabel kind="live" />} />
      <div className="space-y-5 px-5 pb-5">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <KpiTile icon="database" tint="blue" label="Total spend" value={fmtUsd(s.total_usd, 2)} basis={`${fmtInt(s.invocations)} model calls.`} />
          <KpiTile icon="activity" tint="teal" label="Largest run" value={fmtUsd(a.largest_run_spend_usd, 2)} basis={`Run cap ${cap(a.run_budget_usd)}. ${a.largest_run_share_of_budget == null ? "No share, no cap set." : `${fmtShare(a.largest_run_share_of_budget)} of the cap used.`}`} />
          <KpiTile icon="calendar" tint="amber" label="Spent today" value={fmtUsd(a.spent_today_usd, 2)} basis={`Daily cap ${cap(a.daily_budget_usd)}. ${a.daily_share_of_budget == null ? "No share, no cap set." : `${fmtShare(a.daily_share_of_budget)} used.`}`} />
          <KpiTile icon="zap" tint="violet" label="Nebius credit left" value={n.enabled ? fmtUsd(n.remaining_usd, 2) : "Off"} basis={n.enabled ? `${fmtUsd(n.spend_usd, 2)} used of ${fmtUsd(n.credit_usd, 2)}.` : "Nebius is not enabled."} />
        </div>
        <div className="grid gap-4 lg:grid-cols-2 *:min-w-0">
          <div>
            <p className="mb-1 text-ink-muted">Largest run against its cap</p>
            <ProgressBar done={a.largest_run_spend_usd} total={a.run_budget_usd > 0 ? a.run_budget_usd : null} label="Largest run spend against the run cap" tone={(a.largest_run_share_of_budget ?? 0) >= 0.9 ? "danger" : "brand"} />
          </div>
          <div>
            <p className="mb-1 text-ink-muted">Today against the daily cap</p>
            <ProgressBar done={a.spent_today_usd} total={a.daily_budget_usd > 0 ? a.daily_budget_usd : null} label="Spend today against the daily cap" tone={(a.daily_share_of_budget ?? 0) >= 0.9 ? "danger" : "brand"} />
          </div>
        </div>
        <p className="text-xs text-ink-muted">{plain(a.run_budget_basis)}.</p>
        {s.models.length > 0 && (
          <div role="region" aria-label="Spend by model" tabIndex={0} className="overflow-x-auto">
            <table className="w-full min-w-[34rem] border-collapse text-left">
              <caption className="sr-only">Spend by model</caption>
              <thead>
                <tr className="border-b border-line text-ink-muted">
                  <th scope="col" className="px-2 py-1.5 font-medium">Model</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Calls</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Tokens in and out</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Cached</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Failed</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Cost</th>
                </tr>
              </thead>
              <tbody>
                {s.models.map((m) => (
                  <tr key={`${m.provider}/${m.model_id}`} className="tnum border-b border-line last:border-0">
                    <td className="px-2 py-1.5">{m.provider} <span className="font-mono text-xs">{m.model_id}</span></td>
                    <td className="px-2 py-1.5 text-right">{fmtInt(m.calls)}</td>
                    <td className="px-2 py-1.5 text-right">{fmtInt(m.input_tokens)} and {fmtInt(m.output_tokens)}</td>
                    <td className="px-2 py-1.5 text-right">{fmtInt(m.cache_hits)}</td>
                    <td className="px-2 py-1.5 text-right">{fmtInt(m.failed_calls)}</td>
                    <td className="px-2 py-1.5 text-right font-semibold">{fmtUsd(m.cost_usd, 4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Card>
  );
}

function Documents({ d }: { d: Processing["documents"] }) {
  const docs = Object.entries(d.documents_by_status).sort(([a], [b]) => a.localeCompare(b));
  const facts = Object.entries(d.evidence_facts_by_validation_status).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Card as="section" aria-labelledby="docs-h">
      <CardHeader id="docs-h" icon="file" title="Notes and extracted facts" subtitle="Clinical notes by processing status, and the facts read from them by validation status." aside={<DataLabel kind="live" />} />
      <div className="grid gap-5 px-5 pb-5 lg:grid-cols-2 *:min-w-0">
        <div>
          <p className="mb-2">
            <span className="tnum text-2xl font-bold">{fmtInt(d.documents_total)}</span> <span className="text-ink-muted">notes</span>
          </p>
          <ul aria-label="Notes by status" className="space-y-1">
            {docs.length === 0 ? <li className="text-ink-muted">No notes are registered yet.</li> : docs.map(([k, v]) => <li key={k} className="flex justify-between gap-3"><span>{humanize(k)}</span><span className="tnum font-semibold">{fmtInt(v)}</span></li>)}
          </ul>
        </div>
        <div>
          <p className="mb-2">
            <span className="tnum text-2xl font-bold">{fmtInt(d.evidence_facts_total)}</span> <span className="text-ink-muted">facts</span>
          </p>
          <ul aria-label="Facts by validation status" className="space-y-1">
            {facts.length === 0 ? <li className="text-ink-muted">No facts have been extracted yet.</li> : facts.map(([k, v]) => <li key={k} className="flex justify-between gap-3"><StatusBadge status={k} /><span className="tnum font-semibold">{fmtInt(v)}</span></li>)}
          </ul>
        </div>
      </div>
    </Card>
  );
}

/**
 * Admin only (owner decision 2026-11-12): internal backend data is not product content. The monitor renders, and fetches,
 * only when /auth/me lists system.configure. The backend still enforces its own permission on the endpoint.
 */
export function ProcessingMonitor() {
  const allowed = useCan()("system.configure");
  if (!allowed)
    return (
      <EmptyState title="You do not have access to this page" icon="lock">
        The live monitor is for system administrators.
      </EmptyState>
    );
  return <MonitorBody />;
}

function MonitorBody() {
  const res = useApi("processing", (signal) => api<Processing>("/analytics/processing", { signal }));
  const [live, setLive] = useState(true);
  const { reload } = res;

  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, POLL_MS);
    return () => clearInterval(t);
  }, [live, reload]);

  const p = res.data?.data;
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-ink-muted" aria-live="off">
          {p ? `Updated ${fmtDateTime(p.generated_at)}. ` : ""}
          {live ? "Refreshes every 10 seconds." : "Automatic refresh is paused."}
        </p>
        <div className="flex gap-2">
          <button type="button" className={btnSecondary} aria-pressed={!live} onClick={() => setLive((l) => !l)}>
            {live ? "Pause refresh" : "Resume refresh"}
          </button>
          <button type="button" className={btnSecondary} onClick={reload}>
            Refresh now
          </button>
        </div>
      </div>
      {res.error && !p ? (
        isMissingEndpoint(res.error) ? <Unavailable endpoint="GET /api/v1/analytics/processing" /> : <ErrorPanel error={res.error} onRetry={reload} title="Could not load the live monitor" />
      ) : !p ? (
        <div role="status" aria-label="Loading the live monitor" className="space-y-3">
          <div className="skeleton h-40 w-full" />
          <div className="skeleton h-40 w-full" />
        </div>
      ) : (
        <>
          {res.error && <ErrorPanel error={res.error} onRetry={reload} title="The last refresh failed. Showing the previous reading" />}
          <Pipeline p={p.pipeline} />
          <Jobs jobs={p.jobs} />
          <Spend s={p.ai_spend} />
          <Documents d={p.documents} />
        </>
      )}
    </div>
  );
}
