"use client";

import { selectClass } from "@/components/ui";
import Link from "next/link";
import { useState } from "react";
import { api, apiList, apiPost } from "@/lib/api";
import { fmtDate, fmtDateTime, humanize } from "@/lib/format";
import type { Appointment, CareGapDetail, CareGapEvaluation, CareGapRule, Communication, CompletionEvidence } from "@/lib/types";
import { useAction } from "@/lib/use-action";
import { useApi } from "@/lib/use-api";
import { useCan } from "@/components/app-shell";
import { SimulatedBadge } from "@/components/badges";
import { CaseConversation } from "@/components/conversation";
import { GatedButton } from "@/components/gated-button";
import { StatusBadge } from "@/components/status-badge";
import { Timeline, type TimelineItem } from "@/components/timeline";
import { AsyncBlock, ErrorPanel, Facts, JsonBlock, PageHeader, PriorityPill, Section, btnPrimary, inputClass } from "@/components/ui";
import { displayText } from "@/lib/display";
import { AppointmentsTable, ClosureSection, EvidenceSection } from "./care-gap-closure";

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
            <select id="next-status" value={next} onChange={(e) => setNext(e.target.value)} className={selectClass}>
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

/** Message body in its own language and direction (Arabic renders right to left), plus any resident responses. */
function outreachItems(list: Communication[]): TimelineItem[] {
  return list.map((o) => ({
    id: o.id,
    at: o.sent_at ?? o.created_at,
    title: (
      <span className="inline-flex flex-wrap items-center gap-2">
        {humanize(o.channel)} <StatusBadge status={o.delivery_state} /> <SimulatedBadge>Simulated message</SimulatedBadge>
      </span>
    ),
    detail: (
      <>
        <span lang={o.language} dir={o.direction} className="block whitespace-pre-wrap text-ink">
          {o.body}
        </span>
        <span className="block text-xs">Template {o.template_key} v{o.template_version}, language {o.language}.</span>
        {(o.responses ?? []).map((r) => (
          <span key={r.id} className="mt-1 block rounded-sm border border-line bg-canvas px-2 py-1 text-ink">
            Resident response (simulated): <span className="font-medium">{humanize(r.response_type)}</span>
            {r.response_value ? `, "${r.response_value}"` : ""}
            <span className="block text-xs text-ink-muted">{fmtDateTime(r.received_at)}</span>
          </span>
        ))}
      </>
    ),
  }));
}

export function CareGapDetailView({ id }: { id: string }) {
  const can = useCan();
  const res = useApi(`care-gap:${id}`, (signal) => api<CareGapDetail>(`/care-gaps/${id}`, { signal }));
  const comms = useApi(`care-gap-comms:${id}`, (signal) => api<Communication[]>(`/care-gaps/${id}/communications`, { signal }));
  const appts = useApi(`care-gap-appts:${id}`, (signal) => apiList<Appointment>("/appointments", { care_gap_case_id: id, page_size: 50 }, signal));
  const evid = useApi(`care-gap-evidence:${id}`, (signal) => api<CompletionEvidence[]>(`/care-gaps/${id}/completion-evidence`, { signal }));
  const refresh = () => {
    res.reload();
    comms.reload();
    appts.reload();
    evid.reload();
  };
  const back = (
    <p className="mb-2">
      <Link href="/care-gaps" className="text-brand-ink underline underline-offset-2">
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

  const rule = g.rule as CareGapRule;
  const ev = g.evaluation as CareGapEvaluation;
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
    detail: [displayText(h.reason), h.actor_role ? `by ${humanize(h.actor_role)}` : "by the system"].filter(Boolean).join(", "),
  }));
  const outreach = outreachItems(comms.data?.data ?? []);

  return (
    <>
      {back}
      <PageHeader
        title={humanize(g.care_activity)}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={g.status} />
            <PriorityPill priority={g.priority} />
            <span className="font-mono text-xs">{g.id}</span>
          </span>
        }
      />

      <div className="grid gap-6 xl:grid-cols-[22rem_minmax(0,1fr)] [&>*]:min-w-0">
        <div>
          <Section id="case" title="Case">
            <Facts
              items={[
                [
                  "Resident",
                  <Link key="r" href={`/residents/${g.resident_id}`} className="font-mono text-brand-ink underline underline-offset-2">
                    {g.resident_id.slice(0, 8)}
                  </Link>,
                ],
                ["Rule outcome", <StatusBadge key="o" status={g.source_outcome} />],
                ["Guideline", rule.guideline_name ? `${rule.guideline_name}${rule.guideline_version != null ? `, version ${rule.guideline_version}` : ""}` : null],
                ["Rule", rule.name ? `${rule.name}, version ${rule.rule_version ?? "unknown"}` : null],
                ["Source reference", rule.source_reference],
                ["District", g.district_code],
                ["Assigned coordinator", g.assigned_coordinator_id ? <span key="a" className="font-mono text-xs">{g.assigned_coordinator_id.slice(0, 8)}</span> : "Unassigned"],
                ["Planning run", g.planning_run_id ? <span key="p" className="font-mono text-xs">{g.planning_run_id.slice(0, 8)}</span> : "Not yet planned"],
                ["Opened", fmtDateTime(g.opened_at)],
                ["Updated", fmtDateTime(g.updated_at)],
                ["Closed", g.closed_at ? `${fmtDateTime(g.closed_at)}${g.closure_reason ? `: ${displayText(g.closure_reason)}` : ""}` : null],
              ]}
            />
            {rule.disclaimer && <p className="mt-3 text-[0.8125rem] text-ink-muted">{displayText(rule.disclaimer)}</p>}
          </Section>

          <Section id="eligibility" title="Eligibility decision">
            <p className="mb-3 text-ink-muted">
              Decided by the versioned rule, not by an AI model.
              {ev.evaluation_date ? ` Evaluated for ${fmtDate(ev.evaluation_date)}.` : ""}
            </p>
            <div className="space-y-2">
              <JsonBlock label="Explanation" value={ev.explanation} />
              <JsonBlock label="Evidence references" value={ev.evidence_references} />
              <JsonBlock label="Missing information" value={ev.missing_information} />
              <JsonBlock label="Exclusion reasons" value={ev.exclusion_reasons} />
            </div>
          </Section>
        </div>

        <div>
          <Section id="history" title="Status history">
            {history.length ? (
              <div className="card p-5">
                <Timeline label="Status history" items={history} />
              </div>
            ) : (
              <p className="text-ink-muted">No status changes recorded.</p>
            )}
          </Section>

          <Section id="change" title="Change status">
            <ChangeStatus key={g.status} c={g} onDone={refresh} />
          </Section>

          <Section id="outreach" title="Outreach" aside={<SimulatedBadge>Simulated delivery</SimulatedBadge>}>
            <AsyncBlock
              loading={comms.loading && !comms.data}
              error={comms.error}
              onRetry={comms.reload}
              endpoint="GET /api/v1/care-gaps/{id}/communications"
              empty={outreach.length === 0}
              emptyTitle="No outreach recorded"
              emptyHint="Outreach messages and resident responses appear here once the case is planned. No message is sent to a real resident."
            >
              <div className="card p-5">
                <Timeline label="Outreach history" items={outreach} />
              </div>
            </AsyncBlock>
          </Section>

          {can("engagement.view") && (
            <div id="conversation" className="mb-6">
              <CaseConversation caseId={g.id} />
            </div>
          )}

          <Section id="appointments" title="Appointments" aside={<SimulatedBadge>Simulated booking</SimulatedBadge>}>
            <AppointmentsTable rows={appts.data?.data ?? null} loading={appts.loading} error={appts.error} onRetry={appts.reload} onChanged={refresh} />
          </Section>

          <Section id="completion" title="Completion evidence">
            <EvidenceSection c={g} rows={evid.data?.data ?? null} loading={evid.loading} error={evid.error} onRetry={evid.reload} onChanged={refresh} />
          </Section>

          <Section id="closure" title="Closure">
            <ClosureSection c={g} evidence={evid.data?.data ?? null} onChanged={refresh} />
          </Section>
        </div>
      </div>
    </>
  );
}
