"use client";

import { useState } from "react";
import { api, apiPost, type ApiError } from "@/lib/api";
import { fmtDateTime, humanize } from "@/lib/format";
import type { Appointment, CareGapDetail, CompletionEvidence } from "@/lib/types";
import { useAction } from "@/lib/use-action";
import { SimulatedBadge } from "@/components/badges";
import { DataTable, type Column } from "@/components/data-table";
import { GatedButton } from "@/components/gated-button";
import { StatusBadge } from "@/components/status-badge";
import { ErrorPanel, Facts, btnDanger, btnPrimary, btnSecondary, inputClass } from "@/components/ui";

const selectClass = "min-h-9 rounded-sm border border-line-strong bg-surface px-2.5 text-sm text-ink";

// Valid next states per engagement/models.py APPOINTMENT_STATUSES; the backend still validates every change.
const NEXT_APPT: Record<string, string[]> = {
  proposed: ["simulated_confirmed", "cancelled"],
  simulated_confirmed: ["completed", "no_show", "cancelled"],
};
const SUBMIT_PERMS = ["caregaps.manage", "caregaps.close"] as const;
const REVIEW_PERMS = ["caregaps.review", "caregaps.close"] as const;
// backend/anticipa/closure/models.py
const EVIDENCE_SOURCES = ["simulated_procedure_record", "clinical_record", "document", "manual_attestation"] as const;
const EXCEPTION_TYPES = ["resident_declined", "unable_to_contact", "moved_out_of_area", "deceased", "clinically_contraindicated", "completed_elsewhere_unverified", "other"] as const;

function AppointmentAction({ a, onDone }: { a: Appointment; onDone: () => void }) {
  const options = NEXT_APPT[a.appointment_status] ?? [];
  const [next, setNext] = useState(options[0] ?? "");
  const act = useAction();
  if (options.length === 0) return <span className="text-ink-muted">None</span>;
  return (
    <div className="flex flex-col items-start gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={`appt-${a.id}`} className="sr-only">
          New appointment status
        </label>
        <select id={`appt-${a.id}`} value={next} onChange={(e) => setNext(e.target.value)} className={selectClass}>
          {options.map((o) => (
            <option key={o} value={o}>
              {humanize(o)}
            </option>
          ))}
        </select>
        <GatedButton
          permission="caregaps.manage"
          className={btnSecondary}
          disabled={act.pending}
          onClick={async () => {
            if (await act.run(() => api(`/appointments/${a.id}/status`, { method: "PATCH", body: { appointment_status: next } }))) onDone();
          }}
        >
          Apply
        </GatedButton>
      </div>
      {act.error && <ErrorPanel error={act.error} title="The appointment was not updated" />}
    </div>
  );
}

export function AppointmentsTable({ rows, loading, error, onRetry, onChanged }: { rows: Appointment[] | null; loading: boolean; error: ApiError | null; onRetry: () => void; onChanged: () => void }) {
  const cols: Column<Appointment>[] = [
    { key: "when", header: "Slot", className: "whitespace-nowrap tnum", cell: (a) => fmtDateTime(a.start_time) },
    { key: "where", header: "Clinic and service", cell: (a) => [a.clinic_key, a.service_key ? humanize(a.service_key) : null].filter(Boolean).join(", ") || "None" },
    {
      key: "status",
      header: "Status",
      cell: (a) => (
        <span className="inline-flex flex-wrap items-center gap-1.5">
          <StatusBadge status={a.appointment_status} />
          <SimulatedBadge>Simulated booking</SimulatedBadge>
        </span>
      ),
    },
    { key: "why", header: "Reason", cell: (a) => a.status_reason ?? "None" },
    { key: "act", header: "Change status", cell: (a) => <AppointmentAction a={a} onDone={onChanged} /> },
  ];
  return (
    <DataTable
      caption="Appointments"
      columns={cols}
      rows={rows}
      getRowId={(a) => a.id}
      loading={loading}
      error={error}
      onRetry={onRetry}
      unavailable={{ endpoint: "GET /api/v1/appointments" }}
      emptyTitle="No appointment for this case"
      emptyHint="Confirming an allocation in Planning creates a simulated appointment that holds the slot. An appointment alone never completes a case."
    />
  );
}

function ReviewButtons({ c, e, onDone }: { c: string; e: CompletionEvidence; onDone: () => void }) {
  const act = useAction();
  if (e.verification_status !== "pending") return <span className="text-ink-muted">None</span>;
  const run = async (decision: "verified" | "rejected") => {
    if (await act.run(() => apiPost(`/care-gaps/${c}/completion-evidence/${e.id}/review`, { decision, note: null }))) onDone();
  };
  return (
    <div className="flex flex-col items-start gap-1">
      <div className="flex flex-wrap gap-2">
        <GatedButton permission={REVIEW_PERMS} className={btnPrimary} disabled={act.pending} onClick={() => run("verified")}>
          Verify
        </GatedButton>
        <GatedButton permission={REVIEW_PERMS} className={btnDanger} disabled={act.pending} onClick={() => run("rejected")}>
          Reject
        </GatedButton>
      </div>
      {act.error && <ErrorPanel error={act.error} title="The review was not saved" />}
    </div>
  );
}

function SubmitEvidence({ c, onDone }: { c: CareGapDetail; onDone: () => void }) {
  const [source, setSource] = useState<string>(EVIDENCE_SOURCES[0]);
  const [reference, setReference] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const act = useAction();
  return (
    <div className="mt-3 max-w-xl rounded-md border border-line bg-surface p-4">
      <h3 className="mb-2 font-medium">Submit completion evidence</h3>
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label htmlFor="ev-source" className="mb-1 block font-medium">
            Source
          </label>
          <select id="ev-source" value={source} onChange={(e) => setSource(e.target.value)} className={selectClass}>
            {EVIDENCE_SOURCES.map((s) => (
              <option key={s} value={s}>
                {humanize(s)}
              </option>
            ))}
          </select>
        </div>
        <div className="min-w-48 flex-1">
          <label htmlFor="ev-ref" className="mb-1 block font-medium">
            Reference
          </label>
          <input id="ev-ref" value={reference} onChange={(e) => setReference(e.target.value)} className={inputClass} placeholder="Record id or description" aria-invalid={!!localError} aria-describedby={localError ? "ev-ref-err" : undefined} />
        </div>
        <GatedButton
          permission={SUBMIT_PERMS}
          className={btnSecondary}
          disabled={act.pending}
          onClick={async () => {
            if (reference.trim().length < 3) {
              setLocalError("Enter a reference of at least 3 characters.");
              return;
            }
            setLocalError(null);
            if (await act.run(() => apiPost(`/care-gaps/${c.id}/completion-evidence`, { source_type: source, evidence_reference: reference.trim() }))) {
              setReference("");
              onDone();
            }
          }}
        >
          Submit evidence
        </GatedButton>
      </div>
      {localError && (
        <p id="ev-ref-err" role="alert" className="mt-2 text-danger">
          {localError}
        </p>
      )}
      {act.error && (
        <div className="mt-2">
          <ErrorPanel error={act.error} title="The evidence was not saved" />
        </div>
      )}
      <p className="mt-2 text-xs text-ink-muted">New evidence is pending until a different person verifies it.</p>
    </div>
  );
}

export function EvidenceSection({ c, rows, loading, error, onRetry, onChanged }: { c: CareGapDetail; rows: CompletionEvidence[] | null; loading: boolean; error: ApiError | null; onRetry: () => void; onChanged: () => void }) {
  const cols: Column<CompletionEvidence>[] = [
    { key: "src", header: "Source", cell: (e) => humanize(e.source_type) },
    { key: "ref", header: "Reference", cell: (e) => <span className="break-all">{e.evidence_reference}</span> },
    {
      key: "status",
      header: "Verification",
      cell: (e) => (
        <span className="inline-flex flex-wrap items-center gap-1.5">
          <StatusBadge status={e.verification_status} />
          <SimulatedBadge />
        </span>
      ),
    },
    { key: "when", header: "Verified", className: "whitespace-nowrap tnum", cell: (e) => (e.verified_at ? fmtDateTime(e.verified_at) : "None") },
    { key: "note", header: "Reviewer note", cell: (e) => e.reviewer_note ?? "None" },
    { key: "act", header: "Review", cell: (e) => <ReviewButtons c={c.id} e={e} onDone={onChanged} /> },
  ];
  return (
    <>
      <DataTable
        caption="Completion evidence"
        columns={cols}
        rows={rows}
        getRowId={(e) => e.id}
        loading={loading}
        error={error}
        onRetry={onRetry}
        unavailable={{ endpoint: "GET /api/v1/care-gaps/{id}/completion-evidence" }}
        emptyTitle="No completion evidence yet"
        emptyHint="A case is completed only when verified evidence shows the care happened. A message or appointment is not enough."
      />
      <SubmitEvidence c={c} onDone={onChanged} />
    </>
  );
}

export function ClosureSection({ c, evidence, onChanged }: { c: CareGapDetail; evidence: CompletionEvidence[] | null; onChanged: () => void }) {
  const [outcome, setOutcome] = useState<"completed" | "closed_not_completed">("completed");
  const [reason, setReason] = useState("");
  const [exception, setException] = useState<string>(EXCEPTION_TYPES[0]);
  const [localError, setLocalError] = useState<string | null>(null);
  const act = useAction();
  const verified = !!evidence?.some((e) => e.verification_status === "verified");
  const blocked = outcome === "completed" && !verified;

  if (c.closed_at) {
    return (
      <Facts
        items={[
          ["Outcome", <StatusBadge key="s" status={c.status} />],
          ["Closed", fmtDateTime(c.closed_at)],
          ["Reason", c.closure_reason],
        ]}
      />
    );
  }
  return (
    <div className="max-w-xl rounded-md border border-line bg-surface p-4">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label htmlFor="close-outcome" className="mb-1 block font-medium">
            Outcome
          </label>
          <select id="close-outcome" value={outcome} onChange={(e) => setOutcome(e.target.value as typeof outcome)} className={selectClass}>
            <option value="completed">Completed</option>
            <option value="closed_not_completed">Closed, not completed</option>
          </select>
        </div>
        {outcome === "closed_not_completed" && (
          <div>
            <label htmlFor="close-exception" className="mb-1 block font-medium">
              Exception type
            </label>
            <select id="close-exception" value={exception} onChange={(e) => setException(e.target.value)} className={selectClass}>
              {EXCEPTION_TYPES.map((x) => (
                <option key={x} value={x}>
                  {humanize(x)}
                </option>
              ))}
            </select>
          </div>
        )}
        <div className="min-w-48 flex-1">
          <label htmlFor="close-reason" className="mb-1 block font-medium">
            Reason (required)
          </label>
          <input id="close-reason" value={reason} onChange={(e) => setReason(e.target.value)} className={inputClass} aria-invalid={!!localError} aria-describedby={localError ? "close-reason-err" : undefined} />
        </div>
        <GatedButton
          permission="caregaps.close"
          className={btnPrimary}
          disabled={act.pending || blocked}
          onClick={async () => {
            if (reason.trim().length < 3) {
              setLocalError("Add a reason of at least 3 characters.");
              return;
            }
            setLocalError(null);
            const body = outcome === "completed" ? { outcome, reason: reason.trim() } : { outcome, reason: reason.trim(), exception_type: exception };
            if (await act.run(() => apiPost(`/care-gaps/${c.id}/close`, body))) onChanged();
          }}
        >
          Close case
        </GatedButton>
      </div>
      {blocked && <p className="mt-2 text-warn">Completing a case needs verified completion evidence. Submit and verify evidence first, or close it as not completed with a reason.</p>}
      {localError && (
        <p id="close-reason-err" role="alert" className="mt-2 text-danger">
          {localError}
        </p>
      )}
      {act.error && (
        <div className="mt-2">
          <ErrorPanel error={act.error} title="The case was not closed" />
        </div>
      )}
    </div>
  );
}
