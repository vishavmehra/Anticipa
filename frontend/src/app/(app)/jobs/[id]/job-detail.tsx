"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, api, apiPost } from "@/lib/api";
import { fmtDateTime, fmtInt } from "@/lib/format";
import { ACTIVE_JOB_STATUSES, type Job, type JobProgress } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { GatedButton } from "@/components/gated-button";
import { PERM } from "@/lib/nav";
import { StatTile } from "@/components/stat-tile";
import { StatusBadge } from "@/components/status-badge";
import { Timeline, type TimelineItem } from "@/components/timeline";
import { ErrorPanel, PageHeader, ProgressBar, btnDanger, btnPrimary, btnSecondary } from "@/components/ui";
import { displayText } from "@/lib/display";

const POLL_MS = 2000;
const RETRYABLE = ["failed", "partially_completed", "cancelled"];

const asApiError = (e: unknown) => (e instanceof ApiError ? e : new ApiError("INTERNAL_ERROR", "Unexpected client error.", 0));

function Json({ label, value }: { label: string; value: Record<string, unknown> | null | undefined }) {
  if (!value || Object.keys(value).length === 0) return null;
  return (
    <details className="mt-3">
      <summary className="cursor-pointer font-medium">{label}</summary>
      <pre className="mt-1 max-h-64 overflow-auto rounded-sm border border-line bg-canvas p-2 font-mono text-xs">
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  );
}

export function JobDetail({ id }: { id: string }) {
  const job = useApi(`job:${id}`, (signal) => api<Job>(`/jobs/${id}`, { signal }));
  const [live, setLive] = useState<JobProgress | null>(null);
  const [pollError, setPollError] = useState<ApiError | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [act, setAct] = useState<{ pending: boolean; error: ApiError | null; notice: string | null }>({
    pending: false,
    error: null,
    notice: null,
  });

  const base = job.data?.data;
  const status = live?.status ?? base?.status;
  const active = !!status && ACTIVE_JOB_STATUSES.includes(status);
  const reload = job.reload;

  // Progress polling while the job is pending/queued/running; stops itself at a terminal state.
  useEffect(() => {
    if (!active) return;
    const ac = new AbortController();
    const timer = setInterval(async () => {
      if (document.visibilityState === "hidden") return;
      try {
        const res = await api<JobProgress>(`/jobs/${id}/progress`, { signal: ac.signal });
        setLive(res.data);
        setPollError(null);
        if (!ACTIVE_JOB_STATUSES.includes(res.data.status)) reload(); // pull final timestamps/summary
      } catch (e) {
        if (!ac.signal.aborted) setPollError(asApiError(e));
      }
    }, POLL_MS);
    return () => {
      clearInterval(timer);
      ac.abort();
    };
  }, [active, id, reload]);

  async function run(kind: "retry" | "cancel") {
    setAct({ pending: true, error: null, notice: null });
    try {
      await apiPost(`/jobs/${id}/${kind}`);
      setLive(null);
      setConfirmCancel(false);
      setAct({
        pending: false,
        error: null,
        notice: kind === "retry" ? "Retry requested." : "Cancellation requested. The worker stops at its next safe point.",
      });
      reload();
    } catch (e) {
      setAct({ pending: false, error: asApiError(e), notice: null });
    }
  }

  if (job.error) {
    return (
      <>
        <PageHeader title="Job" actions={<Link href="/jobs" className={btnSecondary}>All jobs</Link>} />
        <ErrorPanel error={job.error} onRetry={reload} title="Could not load this job" />
      </>
    );
  }
  if (!base) return <p role="status" className="text-ink-muted">Loading job…</p>;

  const j: Job = { ...base, ...live, id: base.id };
  const tone = j.status === "completed" ? "ok" : j.status === "failed" ? "danger" : j.status === "partially_completed" ? "warn" : "info";
  const timeline: TimelineItem[] = [
    { id: "created", at: j.created_at, title: "Created" },
    { id: "started", at: j.started_at, title: "Started", tone: "info" },
    { id: "updated", at: j.updated_at, title: "Last updated" },
    {
      id: "ended",
      at: j.completed_at,
      title: j.status === "failed" ? "Failed" : j.status === "cancelled" ? "Cancelled" : j.status === "partially_completed" ? "Ended with failures" : "Completed",
      tone: j.status === "completed" ? "ok" : j.status === "failed" ? "danger" : "neutral",
    },
  ].filter((t) => t.at) as TimelineItem[];
  timeline.sort((a, b) => Date.parse(a.at as string) - Date.parse(b.at as string));

  return (
    <>
      <p className="mb-2">
        <Link href="/jobs" className="text-brand-ink underline underline-offset-2">
          All jobs
        </Link>
      </p>
      <PageHeader
        title={j.job_type}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={j.status} />
            <span className="font-mono text-xs">{j.id}</span>
          </span>
        }
        actions={
          <>
            {RETRYABLE.includes(j.status) && (
              <GatedButton permission={PERM.processingExecute} className={btnPrimary} disabled={act.pending} onClick={() => run("retry")}>
                Retry job
              </GatedButton>
            )}
            {active &&
              (confirmCancel ? (
                <>
                  <GatedButton permission={PERM.processingExecute} className={btnDanger} disabled={act.pending} onClick={() => run("cancel")}>
                    Confirm cancel
                  </GatedButton>
                  <button type="button" className={btnSecondary} onClick={() => setConfirmCancel(false)}>
                    Keep running
                  </button>
                </>
              ) : (
                <GatedButton permission={PERM.processingExecute} className={btnSecondary} onClick={() => setConfirmCancel(true)}>
                  Cancel job
                </GatedButton>
              ))}
          </>
        }
      />

      <div aria-live="polite" className="mb-4 space-y-3">
        {act.notice && <p role="status" className="rounded-md border border-ok bg-ok-bg px-4 py-2 text-ok">{act.notice}</p>}
        {act.error && <ErrorPanel error={act.error} title="That action did not go through" />}
        {j.cancel_requested && active && (
          <p role="status" className="rounded-md border border-warn bg-warn-bg px-4 py-2 text-warn">
            Cancellation has been requested. The worker stops at its next safe point.
          </p>
        )}
        {pollError && active && (
          <p role="status" className="rounded-md border border-warn bg-warn-bg px-4 py-2 text-warn">
            Live updates are paused: {pollError.message} Retrying every {POLL_MS / 1000} seconds.
          </p>
        )}
      </div>

      <section aria-label="Progress" className="mb-5">
        <div className="mb-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile label="Processed" value={fmtInt(j.processed_items)} basis={`of ${fmtInt(j.total_items)} items`} />
          <StatTile label="Succeeded" value={fmtInt(j.succeeded_items)} basis="items" />
          <StatTile label="Failed" value={fmtInt(j.failed_items)} basis="items" />
          <StatTile label="Retries" value={fmtInt(j.retry_count)} basis={j.max_retries != null ? `of ${fmtInt(j.max_retries)} allowed` : undefined} />
        </div>
        <ProgressBar done={j.processed_items} total={j.total_items} label="Job progress" tone={tone} />
        {active && <p className="mt-1 text-xs text-ink-muted">Updating every {POLL_MS / 1000} seconds while the job is active.</p>}
      </section>

      {j.error_summary && (
        <div className="mb-5 rounded-md border border-danger bg-danger-bg px-4 py-3 text-danger">
          <h2 className="font-semibold">Error summary</h2>
          <p className="mt-0.5 whitespace-pre-wrap">{displayText(j.error_summary)}</p>
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        <section aria-labelledby="tl" className="rounded-md border border-line bg-surface p-4">
          <h2 id="tl" className="mb-3 text-base font-semibold">Timeline</h2>
          {timeline.length ? <Timeline label="Job lifecycle" items={timeline} /> : <p className="text-ink-muted">No timestamps recorded yet.</p>}
        </section>
        <section aria-labelledby="dt" className="rounded-md border border-line bg-surface p-4">
          <h2 id="dt" className="mb-3 text-base font-semibold">Details</h2>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
            <dt className="text-ink-muted">Created by</dt>
            <dd className="font-mono text-xs leading-5">{j.created_by ?? "None"}</dd>
            <dt className="text-ink-muted">Config version</dt>
            <dd>{j.config_version ?? "None"}</dd>
            <dt className="text-ink-muted">Created</dt>
            <dd className="tnum">{fmtDateTime(j.created_at)}</dd>
          </dl>
          <Json label="Input scope" value={j.input_scope} />
          <Json label="Checkpoint" value={j.checkpoint} />
        </section>
      </div>
    </>
  );
}
