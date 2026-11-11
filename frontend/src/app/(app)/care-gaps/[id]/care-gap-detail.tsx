"use client";

import Link from "next/link";
import { useState } from "react";
import { api, apiList, apiPost } from "@/lib/api";
import { fmtDate, fmtDateTime, humanize } from "@/lib/format";
import type { CareGapDetail, Communication } from "@/lib/types";
import { useAction } from "@/lib/use-action";
import { useApi } from "@/lib/use-api";
import { SimulatedBadge, SyntheticBadge } from "@/components/badges";
import { GatedButton } from "@/components/gated-button";
import { StatusBadge } from "@/components/status-badge";
import { Timeline, type TimelineItem } from "@/components/timeline";
import { AsyncBlock, ErrorPanel, Facts, JsonBlock, PageHeader, Section, btnPrimary, inputClass } from "@/components/ui";

// caregaps.manage / .review / .close: the lifecycle decides which one a given transition needs (lifecycle.py).
const CHANGE_PERMS = ["caregaps.manage", "caregaps.review", "caregaps.close"] as const;
const REASON_REQUIRED = ["escalated", "cancelled", "closed_not_completed"];

function ChangeStatus({ c, onDone }: { c: CareGapDetail; onDone: () => void }) {
  const reopenOnly = c.allowed_transitions.length === 1 && c.allowed_transitions[0] === "reopen";
  const options = c.allowed_transitions.filter((t) => t !== "reopen");
  const [next, setNext] = useState(options[0] ?? "");
  const [reason, setReason] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const act = useAction();

  async function submit() {
    const needs = reopenOnly || REASON_REQUIRED.includes(next);
    if (needs && reason.trim().length < 3) {
      setLocalError("Add a reason of at least 3 characters.");
      return;
    }
    setLocalError(null);
    const ok = await act.run(() =>
      reopenOnly ? apiPost(`/care-gaps/${c.id}/reopen`, { reason: reason.trim() }) : api(`/care-gaps/${c.id}/status`, { method: "PATCH", body: { status: next, reason: reason.trim() || null } }),
    );
    if (ok) {
      setReason("");
      onDone();
    }
  }

  if (!reopenOnly && options.length === 0) return <p className="text-ink-muted">No status changes are possible from here.</p>;
  return (
    <div className="max-w-xl rounded-md border border-line bg-surface p-4">
      <div className="flex flex-wrap items-end gap-3">
        {!reopenOnly && (
          <div>
            <label htmlFor="next-status" className="mb-1 block font-medium">
              New status
            </label>
            <select id="next-status" value={next} onChange={(e) => setNext(e.target.value)} className="min-h-9 rounded-sm border border-line-strong bg-surface px-2.5 text-sm text-ink">
              {options.map((o) => (
                <option key={o} value={o}>
                  {humanize(o)}
                </option>
              ))}
            </select>
          </div>
        )}
        <div className="min-w-48 flex-1">
          <label htmlFor="status-reason" className="mb-1 block font-medium">
            Reason{reopenOnly || REASON_REQUIRED.includes(next) ? " (required)" : ""}
          </label>
          <input
            id="status-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            aria-invalid={!!localError}
            aria-describedby={localError ? "status-reason-err" : undefined}
            className={inputClass}
          />
        </div>
        <GatedButton permission={CHANGE_PERMS} className={btnPrimary} disabled={act.pending} onClick={submit}>
          {reopenOnly ? "Reopen case" : "Update status"}
        </GatedButton>
      </div>
      {localError && (
        <p id="status-reason-err" role="alert" className="mt-2 text-danger">
          {localError}
        </p>
      )}
      {act.error && (
        <div className="mt-2">
          <ErrorPanel error={act.error} title="The status was not changed" />
        </div>
      )}
      <p className="mt-2 text-xs text-ink-muted">Only valid lifecycle transitions are listed. Every change is recorded in the history and the audit log.</p>
    </div>
  );
}

export function CareGapDetailView({ id }: { id: string }) {
  const res = useApi(`care-gap:${id}`, (signal) => api<CareGapDetail>(`/care-gaps/${id}`, { signal }));
  const comms = useApi(`care-gap-comms:${id}`, (signal) => apiList<Communication>(`/care-gaps/${id}/communications`, { page_size: 100 }, signal));
  const back = (
    <p className="mb-2">
      <Link href="/care-gaps" className="text-link underline underline-offset-2">
        All care gaps
      </Link>
    </p>
  );

  if (res.error) {
    return (
      <>
        {back}
        <PageHeader title="Care gap" />
        <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load this care gap" />
      </>
    );
  }
  const g = res.data?.data;
  if (!g) return <p role="status" className="text-ink-muted">Loading care gap…</p>;

  const history: TimelineItem[] = g.history.map((h) => ({
    id: h.id,
    at: h.occurred_at,
    tone: "neutral",
    title: (
      <span className="inline-flex flex-wrap items-center gap-1.5">
        {h.from_status ? <StatusBadge status={h.from_status} /> : <span className="text-ink-muted">Created as</span>}
        {h.from_status && (
          <>
            <span aria-hidden="true">to</span>
            <span className="sr-only">changed to</span>
          </>
        )}
        <StatusBadge status={h.to_status} />
      </span>
    ),
    detail: [h.reason, h.actor_role ? `by ${humanize(h.actor_role)}` : "by the system"].filter(Boolean).join(", "),
  }));
  const outreach: TimelineItem[] = (comms.data?.data ?? []).map((o) => ({
    id: o.id,
    at: o.occurred_at,
    title: (
      <span className="inline-flex items-center gap-2">
        {humanize(o.event_type ?? o.channel ?? "outreach")} <SimulatedBadge />
      </span>
    ),
    detail: o.summary,
  }));

  return (
    <>
      {back}
      <PageHeader
        title={humanize(g.care_activity)}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={g.status} />
            <SyntheticBadge>{humanize(g.synthetic_status)}</SyntheticBadge>
            <span className="font-mono text-xs">{g.id}</span>
          </span>
        }
      />

      <Section id="case" title="Case">
        <Facts
          items={[
            [
              "Resident",
              <Link key="r" href={`/residents/${g.resident_id}`} className="font-mono text-link underline underline-offset-2">
                {g.resident_id.slice(0, 8)}
              </Link>,
            ],
            ["Priority", humanize(g.priority)],
            ["Rule outcome", <StatusBadge key="o" status={g.source_outcome} />],
            ["Guideline", g.rule.guideline_name ? `${g.rule.guideline_name}${g.rule.guideline_version != null ? `, version ${g.rule.guideline_version}` : ""}` : null],
            ["Rule", g.rule.name ? `${g.rule.name}, version ${g.rule.rule_version ?? "?"}` : null],
            ["Source reference", g.rule.source_reference],
            ["District", g.district_code],
            ["Assigned coordinator", g.assigned_coordinator_id ? <span key="a" className="font-mono text-xs">{g.assigned_coordinator_id.slice(0, 8)}</span> : "Unassigned"],
            ["Planning run", g.planning_run_id ? <span key="p" className="font-mono text-xs">{g.planning_run_id.slice(0, 8)}</span> : "Not yet planned"],
            ["Opened", fmtDateTime(g.opened_at)],
            ["Updated", fmtDateTime(g.updated_at)],
            ["Closed", g.closed_at ? `${fmtDateTime(g.closed_at)}${g.closure_reason ? `: ${g.closure_reason}` : ""}` : null],
          ]}
        />
        {g.rule.disclaimer && <p className="mt-2 max-w-[65ch] text-ink-muted">{g.rule.disclaimer}</p>}
      </Section>

      <Section id="history" title="Status history">
        {history.length ? (
          <div className="rounded-md border border-line bg-surface p-4">
            <Timeline label="Status history" items={history} />
          </div>
        ) : (
          <p className="text-ink-muted">No status changes recorded.</p>
        )}
      </Section>

      <Section id="change" title="Change status">
        <ChangeStatus key={g.status} c={g} onDone={res.reload} />
      </Section>

      <Section id="eligibility" title="Eligibility evaluation">
        <p className="mb-2 max-w-[65ch] text-ink-muted">
          Decided by the versioned rule above, not by an AI model.
          {g.evaluation.evaluation_date ? ` Evaluated for ${fmtDate(g.evaluation.evaluation_date)}.` : ""}
        </p>
        <div className="space-y-2">
          <JsonBlock label="Explanation" value={g.evaluation.explanation} />
          <JsonBlock label="Evidence references" value={g.evaluation.evidence_references} />
          <JsonBlock label="Missing information" value={g.evaluation.missing_information} />
          <JsonBlock label="Exclusion reasons" value={g.evaluation.exclusion_reasons} />
        </div>
      </Section>

      <Section id="outreach" title="Outreach" aside={<SimulatedBadge>Simulated messages</SimulatedBadge>}>
        <AsyncBlock
          loading={comms.loading && !comms.data}
          error={comms.error}
          onRetry={comms.reload}
          endpoint="GET /api/v1/care-gaps/{id}/communications"
          empty={outreach.length === 0}
          emptyTitle="No outreach recorded"
          emptyHint="Outreach drafts and resident responses appear here once engagement is connected. Every message is simulated."
        >
          <div className="rounded-md border border-line bg-surface p-4">
            <Timeline label="Outreach history" items={outreach} />
          </div>
        </AsyncBlock>
      </Section>
    </>
  );
}
