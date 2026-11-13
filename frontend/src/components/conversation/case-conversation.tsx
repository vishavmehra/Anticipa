"use client";

// Resident conversation for one care gap case: the simulated two way thread between the resident and the appointment
// assistant. A coordinator types what the resident says (in English, Hindi or Arabic, Arabic right to left); the assistant
// answers appointment logistics only, proposes (never books) another slot, and hands every health matter to a clinician.
// Reads GET /care-gaps/{id}/conversation and posts to /care-gaps/{id}/conversation/messages (backend/anticipa/conversation).
// Every message is simulated and nothing is sent to a real person. Mount: journey ACT panel and the case detail page.

import { useState, type FormEvent, type KeyboardEvent } from "react";
import { ApiError, api, apiPost } from "@/lib/api";
import { displayText } from "@/lib/display";
import { LANGUAGES, fmtDateTime, humanize } from "@/lib/format";
import { useAction } from "@/lib/use-action";
import { useApi } from "@/lib/use-api";
import { useCan } from "@/components/app-shell";
import { Card, CardHeader, DataLabel, EmptyState, ErrorPanel, Icon, Pill, btnPrimary, inputClass, selectClass, type IconName, type Tone } from "@/components/ui";
import type { ConversationChannel, ConversationLanguage, ConversationMessage, ConversationThread, EscalationReason, EscalationState, PostResult } from "./types";

const MAX_CHARS = 600;
const LANGS: ConversationLanguage[] = ["en", "hi", "ar"];
const CHANNELS: { value: "" | ConversationChannel; label: string }[] = [
  { value: "", label: "Same channel as the last message" },
  { value: "sms", label: "SMS" },
  { value: "whatsapp", label: "WhatsApp" },
  { value: "phone_call", label: "Phone call" },
  { value: "email", label: "Email" },
];
// Starting points for a demonstration: they only fill the box. Sending is always a deliberate click.
const QUICK: Record<ConversationLanguage, string[]> = {
  en: ["When is my appointment and where?", "Can I come on a different day?", "I found a lump, what should I do?"],
  hi: ["मेरी अपॉइंटमेंट कब है और कहाँ?", "क्या मैं किसी और दिन आ सकता हूँ?", "मुझे गांठ महसूस हुई है, मैं क्या करूँ?"],
  ar: ["متى موعدي وأين؟", "هل يمكنني تغيير الموعد إلى يوم آخر؟", "وجدت كتلة، ماذا أفعل؟"],
};
const REASON: Record<EscalationReason, { label: string; tone: Tone; icon: IconName }> = {
  emergency: { label: "Emergency", tone: "danger", icon: "alert" },
  medical_question: { label: "Medical question", tone: "warn", icon: "stethoscope" },
  eligibility_dispute: { label: "Eligibility question", tone: "warn", icon: "stethoscope" },
  clinician_flag: { label: "Flagged for a clinician", tone: "warn", icon: "stethoscope" },
  unsafe_reply: { label: "Reply withheld by a safety check", tone: "warn", icon: "shield" },
};
const STATE: Record<EscalationState, string> = {
  escalated: "escalated to a clinician",
  already_escalated: "already escalated",
  case_closed: "case is closed, so it was not escalated",
};
const CHIP: Record<Tone, string> = {
  ok: "bg-ok-bg text-ok",
  warn: "bg-warn-bg text-warn",
  danger: "bg-danger-bg text-danger",
  info: "bg-info-bg text-info",
  neutral: "bg-neutral-bg text-neutral",
  brand: "bg-brand-soft text-brand-ink",
};
/** Like Pill, but the label may wrap onto a second line (a long hand off or consent label on a phone). */
function Chip({ tone, icon, children }: { tone: Tone; icon: IconName; children: React.ReactNode }) {
  return (
    <span className={`inline-flex max-w-full items-start gap-1 rounded-2xl px-2.5 py-0.5 text-xs leading-5 font-semibold ${CHIP[tone]}`}>
      <Icon name={icon} className="mt-0.5 size-3.5" strokeWidth={2.2} />
      <span className="min-w-0">{children}</span>
    </span>
  );
}
const channelLabel = (c: string) => CHANNELS.find((x) => x.value === c)?.label ?? humanize(c);
const when = (m: ConversationMessage) => Date.parse(m.created_at) || 0;

function describeError(e: ApiError): string {
  switch (e.code) {
    case "RATE_LIMITED":
      return "The message limit has been reached for now. Please try again later.";
    case "FORBIDDEN":
      return "Your role cannot add messages to this case.";
    case "UNAUTHENTICATED":
      return "Your session has ended. Sign in again to continue.";
    case "NOT_FOUND":
      return "This case could not be found.";
    case "VALIDATION_ERROR":
      return "That message could not be sent. Check its length and try again.";
    case "NETWORK_ERROR":
      return "The service could not be reached. Check the connection and try again.";
    default:
      return "Something went wrong while sending. Please try again.";
  }
}

function summarise(r: PostResult): string {
  const parts: string[] = [];
  if (r.escalation) parts.push(`${REASON[r.escalation.reason].label}, ${STATE[r.escalation.state]}.`);
  if (r.opt_out) parts.push("Opt out recorded in the consent ledger.");
  if (r.suppressed) parts.push(`Reply not sent: the resident opted out of ${channelLabel(r.channel)}.`);
  else if (r.reply) parts.push("The assistant replied.");
  return parts.join(" ");
}

function Chips({ m }: { m: ConversationMessage }) {
  if (m.role === "resident") {
    const reason = m.escalation_reason ? REASON[m.escalation_reason] : null;
    return (
      <>
        {reason && m.escalation_state && (
          <Chip tone={m.escalation_state === "case_closed" ? "warn" : reason.tone} icon={reason.icon}>
            {reason.label}, {STATE[m.escalation_state]}
          </Chip>
        )}
        {m.intent === "opt_out" && (
          <Chip tone="info" icon="ban">
            Opt out recorded
          </Chip>
        )}
        {m.reply_sent === false && (
          <Chip tone="warn" icon="ban">
            Reply not sent, resident opted out of this channel
          </Chip>
        )}
      </>
    );
  }
  return m.source === "model" ? (
    <Chip tone="brand" icon="sparkles">
      Written by the assistant
    </Chip>
  ) : (
    <Chip tone="neutral" icon="shield">
      Approved text
    </Chip>
  );
}

function Proposal({ p }: { p: NonNullable<ConversationMessage["proposal"]> }) {
  return (
    <div className="mt-2 flex items-start gap-2 rounded-control border border-dashed border-line-strong bg-surface px-3 py-2.5 text-[0.875rem]">
      <Icon name="calendar-check" className="mt-0.5 size-4 text-brand-ink" />
      <div className="min-w-0">
        <p className="font-semibold">
          Proposed slot: {p.date}, {p.start} to {p.end}
          {p.clinic ? ` at ${p.clinic}` : ""}
        </p>
        <p className="text-ink-muted">Not booked. A care coordinator must confirm it with the resident.</p>
      </div>
    </div>
  );
}

function Bubble({ m }: { m: ConversationMessage }) {
  const resident = m.role === "resident";
  return (
    <li className={`flex ${resident ? "justify-start" : "justify-end"}`}>
      <div className={`w-full max-w-[40rem] rounded-card border px-4 py-3 ${resident ? "border-line bg-surface" : "border-brand/20 bg-brand-soft"}`}>
        <div className="flex flex-wrap items-center gap-1.5 text-[0.8125rem]">
          <span className="inline-flex items-center gap-1.5 font-semibold">
            <Icon name={resident ? "user" : "message"} className="size-4" />
            {resident ? "Resident" : "Assistant"}
          </span>
          <Pill tone="neutral" icon="languages">
            {LANGUAGES[m.language] ?? m.language}
          </Pill>
          <DataLabel kind="simulated">Simulated</DataLabel>
          <Chips m={m} />
          <time dateTime={m.created_at} className="ml-auto text-ink-muted">
            {fmtDateTime(m.created_at)}
          </time>
        </div>
        <p lang={m.language} dir={m.direction} className="mt-2 text-[0.9375rem] leading-6 whitespace-pre-wrap">
          {resident ? m.body : displayText(m.body)}
        </p>
        {m.proposal && <Proposal p={m.proposal} />}
      </div>
    </li>
  );
}

/**
 * The conversation card for one case. `canSend` overrides the permission check (caregaps.manage from GET /auth/me); leave it
 * out in the app, where observers and other read only roles see the thread without the message box.
 */
export function CaseConversation({ caseId, canSend }: { caseId: string; canSend?: boolean }) {
  const can = useCan();
  const allowed = canSend ?? can("caregaps.manage");
  const thread = useApi(`conversation:${caseId}`, async (signal) => (await api<ConversationThread>(`/care-gaps/${caseId}/conversation`, { signal })).data);
  const action = useAction();
  const [draft, setDraft] = useState("");
  const [lang, setLang] = useState<ConversationLanguage>("en");
  const [channel, setChannel] = useState<"" | ConversationChannel>("");
  const [added, setAdded] = useState<ConversationMessage[]>([]); // what the last posts returned, shown before the thread reloads
  const [sending, setSending] = useState<string | null>(null);
  const [announce, setAnnounce] = useState("");

  const data = thread.data;
  const byId = new Map<string, ConversationMessage>();
  for (const m of [...(data?.messages ?? []), ...added]) byId.set(m.id, m);
  const messages = [...byId.values()].sort((a, b) => when(a) - when(b) || (a.role === b.role ? 0 : a.role === "resident" ? -1 : 1));
  const denied = data?.consent.denied_channels ?? [];

  const send = async () => {
    const text = draft.trim().replace(/\s+/g, " ");
    if (!text || sending) return;
    setSending(text);
    setAnnounce("");
    const done = await action.run(async () => {
      const res = await apiPost<PostResult>(`/care-gaps/${caseId}/conversation/messages`, { text, language: lang, ...(channel ? { channel } : {}) });
      setAdded((prev) => [...prev, res.data.resident_message, ...(res.data.reply ? [res.data.reply] : [])]);
      setAnnounce(summarise(res.data));
    });
    setSending(null);
    if (done) {
      setDraft("");
      thread.reload();
    }
  };
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void send();
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <Card as="section" aria-labelledby={`conv-title-${caseId}`}>
      <CardHeader
        id={`conv-title-${caseId}`}
        title="Conversation"
        subtitle="Two way messages with the resident, entered by a coordinator. The assistant answers appointment questions only."
        icon="message"
      />
      <div className="space-y-3 px-5 pb-5">
        <div className="flex flex-wrap items-center gap-2">
          <DataLabel kind="simulated">Simulated conversation</DataLabel>
          {data?.escalated && (
            <Chip tone="warn" icon="stethoscope">
              Case escalated, waiting for a clinician
            </Chip>
          )}
        </div>
        {data?.consent.outreach_blocked ? (
          <p role="status" className="flex items-center gap-2 rounded-control border border-warn-line bg-warn-bg px-3 py-2 text-[0.875rem] text-warn">
            <Icon name="ban" className="size-4" />
            The resident opted out of outreach on every channel. No replies are sent.
          </p>
        ) : denied.length > 0 ? (
          <p role="status" className="flex items-center gap-2 rounded-control border border-warn-line bg-warn-bg px-3 py-2 text-[0.875rem] text-warn">
            <Icon name="ban" className="size-4" />
            The resident opted out of {denied.map(channelLabel).join(" and ")}. No replies are sent on {denied.length === 1 ? "that channel" : "those channels"}.
          </p>
        ) : null}

        {thread.error && !data ? (
          <ErrorPanel error={thread.error} onRetry={thread.reload} title="Could not load the conversation" />
        ) : thread.loading && !data ? (
          <div role="status" aria-label="Loading the conversation" className="space-y-2">
            {[0, 1].map((i) => (
              <div key={i} className="skeleton h-16 w-full rounded-card" />
            ))}
          </div>
        ) : (
          <div role="log" aria-live="polite" aria-label="Conversation thread" tabIndex={0} className="scroll-quiet max-h-[28rem] min-h-24 overflow-y-auto rounded-control p-1">
            <ol className="space-y-3">
            {messages.length === 0 && !sending && (
              <li>
                <EmptyState title="No messages yet" icon="message">
                  Type what the resident says below. Appointment questions get an answer; every health question goes to a clinician.
                </EmptyState>
              </li>
            )}
            {messages.map((m) => (
              <Bubble key={m.id} m={m} />
            ))}
            {sending && (
              <li className="flex justify-start" aria-label="Sending">
                <div className="w-full max-w-[40rem] rounded-card border border-dashed border-line-strong bg-surface px-4 py-3">
                  <p className="flex items-center gap-1.5 text-[0.8125rem] font-semibold">
                    <Icon name="user" className="size-4" /> Resident
                    <Pill tone="neutral" icon="clock">
                      Preparing a reply
                    </Pill>
                  </p>
                  <p lang={lang} dir={lang === "ar" ? "rtl" : "ltr"} className="mt-2 text-[0.9375rem] leading-6 whitespace-pre-wrap">
                    {sending}
                  </p>
                </div>
              </li>
            )}
            </ol>
          </div>
        )}

        <p role="status" aria-live="polite" className="text-[0.8125rem] text-ink-muted empty:hidden">
          {announce}
        </p>

        {allowed ? (
          <form onSubmit={onSubmit} aria-label="Add a resident message" className="space-y-3 border-t border-line pt-3">
            <div className="flex flex-wrap items-end gap-x-5 gap-y-3">
              <fieldset>
                <legend className="mb-1 text-[0.8125rem] font-semibold text-ink-soft">Language of the message</legend>
                <div className="glass-strong inline-flex gap-1 rounded-full p-1" role="radiogroup" aria-label="Language of the message">
                  {LANGS.map((l) => (
                    <label key={l} title={LANGUAGES[l]} className="relative cursor-pointer">
                      <input type="radio" name={`conv-lang-${caseId}`} value={l} checked={lang === l} onChange={() => setLang(l)} aria-label={LANGUAGES[l]} className="peer sr-only" />
                      <span className="inline-flex min-h-9 min-w-12 items-center justify-center rounded-full px-3 text-sm font-semibold text-ink-soft peer-checked:bg-brand peer-checked:text-white peer-focus-visible:ring-2 peer-focus-visible:ring-brand peer-focus-visible:ring-offset-2">
                        {l.toUpperCase()}
                      </span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <div className="min-w-48 flex-1">
                <label htmlFor={`conv-channel-${caseId}`} className="mb-1 block text-[0.8125rem] font-semibold text-ink-soft">
                  Channel the resident wrote on
                </label>
                <select id={`conv-channel-${caseId}`} value={channel} onChange={(e) => setChannel(e.target.value as "" | ConversationChannel)} className={selectClass}>
                  {CHANNELS.map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div>
              <label htmlFor={`conv-text-${caseId}`} className="mb-1 block text-[0.8125rem] font-semibold text-ink-soft">
                Resident message
              </label>
              <textarea
                id={`conv-text-${caseId}`}
                value={draft}
                onChange={(e) => setDraft(e.target.value.slice(0, MAX_CHARS))}
                onKeyDown={onKey}
                rows={2}
                lang={lang}
                dir={lang === "ar" ? "rtl" : "ltr"}
                aria-describedby={`conv-hint-${caseId}`}
                placeholder="Type what the resident says"
                className={`${inputClass} min-h-20 resize-y py-2.5`}
              />
              <p id={`conv-hint-${caseId}`} className="mt-1 flex justify-between gap-3 text-[0.75rem] text-ink-muted">
                <span>Simulated: nothing is sent to a real person. Enter sends, Shift and Enter adds a line.</span>
                {draft.length > 480 && <span aria-live="polite">{MAX_CHARS - draft.length} characters left</span>}
              </p>
            </div>
            {action.error && (
              <p role="alert" className="flex items-center gap-2 rounded-control border border-danger-line bg-danger-bg px-3 py-2 text-[0.875rem] text-danger">
                <Icon name="alert" className="size-4" />
                {describeError(action.error)}
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <button type="submit" disabled={!draft.trim() || !!sending} className={btnPrimary}>
                <Icon name="zap" className="size-4" />
                {sending ? "Sending" : "Send as resident"}
              </button>
              <span className="text-[0.8125rem] text-ink-muted">Examples:</span>
              {QUICK[lang].map((q) => (
                <button key={q} type="button" onClick={() => setDraft(q)} lang={lang} dir={lang === "ar" ? "rtl" : "ltr"} className="rounded-full border border-line-strong bg-[var(--control-bg)] px-3 py-1.5 text-[0.8125rem] font-medium text-ink hover:bg-brand-soft">
                  {q}
                </button>
              ))}
            </div>
          </form>
        ) : (
          <p className="flex items-center gap-2 border-t border-line pt-3 text-[0.875rem] text-ink-muted">
            <Icon name="lock" className="size-4" />
            Read only access: you can follow this conversation but not add messages.
          </p>
        )}
      </div>
    </Card>
  );
}
