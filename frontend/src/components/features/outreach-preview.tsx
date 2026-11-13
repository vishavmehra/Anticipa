"use client";

// Multilingual outreach preview: the approved reminder template for one case, side by side in English, Hindi and Arabic
// (Arabic right to left), plus the simulated delivery timeline of the messages already recorded for the case. Reads
// GET /engagement/preview?case_id= (backend/anticipa/analytics/journey.py `engagement_preview`; no AI call, nothing is
// stored or sent). The timeline uses the real timestamps of recorded messages. Route use: journey ACT panel.
// Purpose: show judges what a resident would receive, in the resident's own language.

import { api, isMissingEndpoint } from "@/lib/api";
import { LANGUAGES, fmtDateTime, humanize } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { StatusBadge } from "@/components/status-badge";
import { Card, CardHeader, DataLabel, ErrorPanel, Pill, Unavailable, Icon } from "@/components/ui";
import type { JourneyMessage, OutreachPreviewData } from "./payloads";
import { dirFor, plain } from "./shared";

const ORDER = ["en", "hi", "ar"];

function Timeline({ m }: { m: JourneyMessage }) {
  const steps: { label: string; at: string | null }[] = [
    { label: "Message created", at: m.created_at },
    { label: "Sent", at: m.sent_at },
    { label: "Delivered", at: m.delivered_at },
    { label: "Resident replied", at: m.response_received_at },
  ];
  const failed = m.delivery_state === "failed";
  return (
    <li className="rounded-control border border-dashed border-specimen bg-surface px-4 py-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="font-semibold">{humanize(m.channel)}</span>
        <span className="text-ink-muted">in {LANGUAGES[m.language] ?? m.language}</span>
        <StatusBadge status={m.delivery_state} />
        <DataLabel kind="simulated">Simulated delivery</DataLabel>
      </div>
      <ol aria-label={`Delivery steps for the ${humanize(m.channel)} message`} className="grid gap-2 sm:grid-cols-4">
        {steps.map((s, i) => {
          const done = s.at != null && !(failed && i >= 2);
          return (
            <li key={s.label} data-done={done} className="flex items-start gap-2">
              <span aria-hidden="true" className={`mt-0.5 grid size-5 shrink-0 place-items-center rounded-full border-2 ${done ? "border-brand bg-brand text-white" : "border-dashed border-line-strong bg-surface text-ink-muted"}`}>
                {done ? <Icon name="check" className="size-3" /> : null}
              </span>
              <span>
                <span className="block font-medium">{s.label}</span>
                <span className="block text-xs text-ink-muted">{failed && i >= 2 ? "Did not happen" : s.at ? fmtDateTime(s.at) : "Not yet"}</span>
              </span>
            </li>
          );
        })}
      </ol>
      {m.responses.length > 0 && (
        <p className="mt-2 text-xs text-ink-muted">
          Reply recorded: {m.responses.map((r) => humanize(r.response_type)).join(", ")}.
        </p>
      )}
    </li>
  );
}

export function OutreachPreview({ caseId, messages = [] }: { caseId: string; messages?: JourneyMessage[] }) {
  const res = useApi(`preview:${caseId}`, (signal) => api<OutreachPreviewData>("/engagement/preview", { query: { case_id: caseId, languages: "en,hi,ar" }, signal }));
  const d = res.data?.data;

  let body;
  if (res.error) {
    body = isMissingEndpoint(res.error) ? <Unavailable endpoint="GET /api/v1/engagement/preview" /> : <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load the reminder preview" />;
  } else if (!d) {
    body = <div role="status" aria-label="Loading the reminder preview" className="skeleton mx-5 mb-5 h-40" />;
  } else {
    const previews = [...d.previews].sort((a, b) => ORDER.indexOf(a.language) - ORDER.indexOf(b.language));
    body = (
      <div className="space-y-5 px-5 pb-5">
        <p className="text-ink-soft">
          Reminder for <span className="font-semibold">{humanize(d.care_activity)}</span>. The resident&apos;s language is{" "}
          <span className="font-semibold">{d.resident_preferred_language ? (LANGUAGES[d.resident_preferred_language] ?? d.resident_preferred_language) : "not recorded"}</span>.{" "}
          {d.resident_preferred_language == null ? `The default is ${LANGUAGES[d.default_language] ?? d.default_language}.` : ""}
        </p>
        <ul aria-label="Reminder in each language" className="grid gap-4 lg:grid-cols-3 *:min-w-0">
          {previews.map((p) => (
            <li key={p.language} data-language={p.language} className="flex flex-col rounded-card border border-line bg-canvas p-4">
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                <span className="font-semibold">{LANGUAGES[p.language] ?? p.language}</span>
                {p.is_resident_preferred_language && <Pill tone="brand" icon="check-circle">Resident&apos;s language</Pill>}
                <span className="text-xs text-ink-muted">{p.direction === "rtl" ? "Reads right to left" : "Reads left to right"}</span>
              </div>
              <p lang={p.language} dir={p.direction === "rtl" ? "rtl" : dirFor(p.language)} className="flex-1 rounded-card border border-line bg-surface px-4 py-3 text-[0.9375rem] leading-7 whitespace-pre-wrap">
                {p.body}
              </p>
              <p className="mt-2 text-xs text-ink-muted">
                {p.characters} characters. Activity named as <span lang={p.language} dir={dirFor(p.language)} className="font-medium text-ink">{p.activity_label}</span>.{" "}
                {p.constraints_checked ? "Checks passed." : "Checks not run."} {p.template_approved === true ? "Template approved." : p.template_stored ? "Template not approved." : "Template not stored yet."}
              </p>
            </li>
          ))}
        </ul>
        <p className="text-xs text-ink-muted">{plain(d.notice)} Template {d.template_key} version {d.template_version}. AI calls made: {d.ai_calls}.</p>
        <div>
          <h3 className="mb-2 font-semibold">Delivery timeline</h3>
          {messages.length === 0 ? (
            <p className="rounded-control border border-dashed border-line-strong px-4 py-4 text-ink-muted">No reminder has been recorded for this case yet. Each simulated message and its delivery steps appear here.</p>
          ) : (
            <ul aria-label="Simulated delivery timeline" className="space-y-2">
              {messages.map((m) => (
                <Timeline key={m.id} m={m} />
              ))}
            </ul>
          )}
        </div>
      </div>
    );
  }
  return (
    <Card as="section" aria-labelledby="preview-h">
      <CardHeader id="preview-h" icon="languages" title="Reminder in three languages" subtitle="The approved wording the resident would receive. Nothing is sent from this screen." aside={<DataLabel kind="simulated" />} />
      {body}
    </Card>
  );
}
