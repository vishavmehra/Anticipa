"use client";

import { useState } from "react";
import { api, apiPost } from "@/lib/api";
import { fmtDateTime, humanize } from "@/lib/format";
import { useAction } from "@/lib/use-action";
import { useApi } from "@/lib/use-api";
import { useCan } from "@/components/app-shell";
import { GatedButton } from "@/components/gated-button";
import { AsyncBlock, ErrorPanel, Notice, Section, btnSecondary, inputClass, selectClass } from "@/components/ui";
import type { ConsentChannel, ConsentInput, ConsentRecord, ConsentScope, ConsentStatus, ConsentView } from "./types";

// Mount on the resident detail page, for example next to the care gaps: <ConsentSection residentId={r.id} />.
// Reads GET /residents/{id}/consents (consent.view, every role). Recording needs consent.manage (care coordinator and
// population administrator); everyone else sees the same rows with disabled buttons. Records are append only: opting
// back in records the opposite decision and the history below keeps both. ADR-070.

const MANAGE = "consent.manage";
const CHANNELS: { key: ConsentChannel; label: string }[] = [
  { key: "sms", label: "SMS" },
  { key: "whatsapp", label: "WhatsApp" },
  { key: "phone_call", label: "Phone call" },
  { key: "email", label: "Email" },
];
const SOURCES = [
  { key: "resident_request", label: "Resident request" },
  { key: "coordinator_entry", label: "Coordinator entry" },
] as const;

type Source = (typeof SOURCES)[number]["key"];
interface Row {
  id: string;
  label: string;
  hint?: string;
  allowed: boolean;
  scope: ConsentScope;
  channel: ConsentChannel | null;
  locked?: string; // why there is no button (covered by a wider opt-out)
}

const inForce = (v: ConsentView, scope: ConsentScope, channel: ConsentChannel | null): ConsentRecord | undefined =>
  v.records.find((r) => r.in_force && r.scope === scope && r.channel === channel);

function rowsFor(v: ConsentView): Row[] {
  const allOutreachDenied = inForce(v, "outreach", null)?.status === "deny";
  const denied = new Set(v.effective.outreach.denied_channels);
  return [
    { id: "all", label: "All outreach", hint: "Every channel below", allowed: !allOutreachDenied, scope: "outreach", channel: null },
    ...CHANNELS.map<Row>((c) => {
      const own = inForce(v, "outreach", c.key)?.status === "deny";
      const viaAll = denied.has(c.key) && !own;
      return {
        id: c.key,
        label: c.label,
        allowed: !denied.has(c.key),
        scope: "outreach",
        channel: c.key,
        locked: viaAll ? "Covered by the opt out of all outreach" : undefined,
      };
    }),
    {
      id: "ai",
      label: "AI processing of documents",
      hint: "Whether documents may be read by an AI model",
      allowed: v.effective.data_use_ai_processing === "permit",
      scope: "data_use_ai_processing",
      channel: null,
    },
  ];
}

function State({ allowed }: { allowed: boolean }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${allowed ? "bg-ok-bg text-ok" : "bg-danger-bg text-danger"}`}>
      {allowed ? "Allowed" : "Opted out"}
    </span>
  );
}

function describe(r: ConsentRecord): string {
  const what = r.scope === "data_use_ai_processing" ? "AI processing of documents" : r.channel ? `${humanize(r.channel)} outreach` : "All outreach";
  return `${r.status === "deny" ? "Opted out of" : "Allowed"} ${what.toLowerCase()}`;
}

export function ConsentSection({ residentId }: { residentId: string }) {
  const res = useApi(`consent:${residentId}`, (signal) => api<ConsentView>(`/residents/${residentId}/consents`, { signal }));
  const can = useCan();
  const act = useAction();
  const [source, setSource] = useState<Source>("resident_request");
  const [note, setNote] = useState("");
  const view = res.data?.data;

  async function record(scope: ConsentScope, status: ConsentStatus, channel: ConsentChannel | null) {
    const body: ConsentInput = { scope, status, source, ...(channel ? { channel } : {}), ...(note.trim() ? { note: note.trim() } : {}) };
    if (await act.run(() => apiPost(`/residents/${residentId}/consents`, body))) {
      setNote("");
      res.reload();
    }
  }

  return (
    <Section id="consent" title="Consent and contact preferences">
      <AsyncBlock
        loading={res.loading && !view}
        error={res.error}
        onRetry={res.reload}
        endpoint="GET /api/v1/residents/{id}/consents"
        empty={!view}
        emptyTitle="No consent information"
      >
        {view && (
          <div className="card space-y-4 p-5">
            {view.effective.outreach.blocked && (
              <Notice tone="warn">
                This resident opted out of every outreach channel. Planning leaves their cases unallocated (reason: Consent denied) and no outreach can be recorded.
              </Notice>
            )}
            <p className="max-w-[65ch] text-ink-muted">{view.default_note} Opting out of a single channel keeps the others open; opting out of all outreach closes every channel.</p>

            {can(MANAGE) && (
              <div className="flex flex-wrap items-end gap-3">
                <label className="flex flex-col gap-1 text-xs font-semibold text-ink-soft">
                  Recorded from
                  <select value={source} onChange={(e) => setSource(e.target.value as Source)} className={selectClass}>
                    {SOURCES.map((s) => (
                      <option key={s.key} value={s.key}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex min-w-56 flex-1 flex-col gap-1 text-xs font-semibold text-ink-soft">
                  Note (optional, no personal details)
                  <input value={note} maxLength={300} onChange={(e) => setNote(e.target.value)} className={inputClass} />
                </label>
              </div>
            )}

            <ul className="divide-y divide-line">
              {rowsFor(view).map((row) => (
                <li key={row.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                  <div className="min-w-0">
                    <p className="font-medium">{row.label}</p>
                    {(row.locked ?? row.hint) && <p className="text-xs text-ink-muted">{row.locked ?? row.hint}</p>}
                  </div>
                  <div className="flex items-center gap-3">
                    <State allowed={row.allowed} />
                    {!row.locked && (
                      <GatedButton permission={MANAGE} className={btnSecondary} disabled={act.pending} onClick={() => record(row.scope, row.allowed ? "deny" : "permit", row.channel)}>
                        {row.allowed ? "Opt out" : "Opt back in"}
                        <span className="sr-only"> of {row.label}</span>
                      </GatedButton>
                    )}
                  </div>
                </li>
              ))}
            </ul>

            {act.error && <ErrorPanel error={act.error} title="The decision was not recorded" />}

            <details>
              <summary className="cursor-pointer text-brand-ink underline underline-offset-2">History ({view.records.length})</summary>
              {view.records.length === 0 ? (
                <p className="mt-2 text-ink-muted">No decision has been recorded. Until one is, outreach and AI processing are allowed.</p>
              ) : (
                <ul className="mt-2 space-y-2">
                  {view.records.map((r) => (
                    <li key={r.id} className="rounded-control border border-line px-3 py-2">
                      <p className="font-medium">
                        {describe(r)} <span className="text-xs font-normal text-ink-muted">{r.in_force ? "In force" : "Superseded or not in effect"}</span>
                      </p>
                      <p className="text-xs text-ink-muted">
                        {fmtDateTime(r.effective_from)}
                        {r.effective_to ? ` until ${fmtDateTime(r.effective_to)}` : ""}, {humanize(r.source)}
                        {r.note ? `, ${r.note}` : ""}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </details>
          </div>
        )}
      </AsyncBlock>
    </Section>
  );
}
