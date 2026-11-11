"use client";

import Link from "next/link";
import { useState } from "react";
import { api, apiPost } from "@/lib/api";
import { fmtDateTime, guessLang, humanize } from "@/lib/format";
import { PERM } from "@/lib/nav";
import type { EvidenceDetail, EvidenceFact, EvidenceSource } from "@/lib/types";
import { useAction } from "@/lib/use-action";
import { useApi } from "@/lib/use-api";
import { useCan } from "./app-shell";
import { SyntheticBadge } from "./badges";
import { EvidenceQuote } from "./evidence-quote";
import { GatedButton } from "./gated-button";
import { StatusBadge } from "./status-badge";
import { ErrorPanel, btnDanger, btnPrimary, btnSecondary, inputClass } from "./ui";

const isVerified = (s: EvidenceSource) => s.verification_status.startsWith("verified_");

/** The quote on its own, with language/direction set so Arabic and Devanagari shape correctly. */
function PlainQuote({ quote, lang }: { quote: string; lang?: string }) {
  return (
    <blockquote lang={lang} dir={lang === "ar" ? "rtl" : "auto"} className="rounded-md border border-line bg-paper px-3 py-2 leading-7">
      {quote}
    </blockquote>
  );
}

/** Fetches the text around the quote only when asked (GET /evidence/{id}?context=N), then highlights the span in it. */
function DocumentQuote({ factId, src }: { factId: string; src: EvidenceSource }) {
  const res = useApi(`evidence-context:${factId}`, (signal) =>
    api<EvidenceDetail>(`/evidence/${factId}`, { query: { context: 400 }, signal }),
  );
  const fallback = src.quote ? <PlainQuote quote={src.quote} lang={guessLang(src.quote)} /> : null;
  if (res.error) {
    return (
      <div className="space-y-2">
        <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load the document context" />
        {fallback}
      </div>
    );
  }
  if (!res.data) return <p role="status" className="text-ink-muted">Loading document context…</p>;
  const d = res.data.data;
  const c = d.context.find((x) => x.source_id === src.id);
  if (!c) {
    return (
      <div className="space-y-2">
        <p className="text-ink-muted">No surrounding text is available for this source.</p>
        {fallback}
      </div>
    );
  }
  const start = Array.from(c.before).length;
  return (
    <EvidenceQuote
      text={c.before + c.quote + c.after}
      start={start}
      end={start + Array.from(c.quote).length}
      lang={d.document?.language ?? guessLang(c.before + c.quote + c.after)}
      shift={(src.start_offset ?? start) - start}
    />
  );
}

function Source({ factId, src }: { factId: string; src: EvidenceSource }) {
  const [open, setOpen] = useState(false);
  const locatable = !!src.document_id && src.start_offset != null && src.end_offset != null;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={src.verification_status} />
        {src.location_ambiguous && <span className="text-xs text-warn">The quote appears more than once in the document.</span>}
      </div>
      {open && locatable ? (
        <DocumentQuote factId={factId} src={src} />
      ) : src.quote ? (
        <PlainQuote quote={src.quote} lang={guessLang(src.quote)} />
      ) : (
        <p className="text-ink-muted">No quote is stored for this source.</p>
      )}
      {locatable && (
        <button type="button" className={btnSecondary} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "Hide document context" : "Show in document"}
        </button>
      )}
      {!isVerified(src) && (
        <p className="text-warn">This quote could not be verified against its source text, so the fact cannot be accepted.</p>
      )}
    </div>
  );
}

function ReviewControls({ fact, onDone }: { fact: EvidenceFact; onDone: () => void }) {
  const can = useCan()(PERM.evidenceReview);
  const [note, setNote] = useState("");
  const [noteError, setNoteError] = useState<string | null>(null);
  const act = useAction();
  const verified = fact.sources.some(isVerified);

  async function decide(decision: "accepted" | "rejected") {
    if (decision === "rejected" && !note.trim()) {
      setNoteError("Add a short reason before rejecting.");
      return;
    }
    setNoteError(null);
    if (await act.run(() => apiPost(`/evidence/${fact.id}/review`, { decision, review_note: note.trim() || null }))) onDone();
  }

  return (
    <div className="mt-3 border-t border-line pt-3">
      {can && (
        <div className="mb-2 max-w-xl">
          <label htmlFor={`note-${fact.id}`} className="mb-1 block font-medium">
            Review note
          </label>
          <input
            id={`note-${fact.id}`}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            aria-invalid={!!noteError}
            aria-describedby={noteError ? `note-err-${fact.id}` : undefined}
            className={inputClass}
            placeholder="Why this decision (required to reject)"
          />
          {noteError && (
            <p id={`note-err-${fact.id}`} role="alert" className="mt-1 text-danger">
              {noteError}
            </p>
          )}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <GatedButton permission={PERM.evidenceReview} className={btnPrimary} disabled={act.pending || !verified} onClick={() => decide("accepted")}>
          Accept
        </GatedButton>
        <GatedButton permission={PERM.evidenceReview} className={btnDanger} disabled={act.pending} onClick={() => decide("rejected")}>
          Reject
        </GatedButton>
        {can && !verified && <span className="text-xs text-ink-muted">Accept is unavailable until a quote is verified.</span>}
      </div>
      {act.error && (
        <div className="mt-2">
          <ErrorPanel error={act.error} title="The review was not saved" />
        </div>
      )}
    </div>
  );
}

/** One extracted fact with its source quote(s). Pass `onReview` to add accept/reject (gated on evidence.review). */
export function EvidenceCard({ fact, onReview, showResident = false }: { fact: EvidenceFact; onReview?: () => void; showResident?: boolean }) {
  const original = typeof fact.value?.original_text === "string" ? fact.value.original_text : null;
  const subject =
    fact.subject_type === "family_member" ? `Relative: ${humanize(fact.relationship_type)}` : humanize(fact.subject_type);
  return (
    <article className="rounded-md border border-line bg-surface p-4" aria-label={`${humanize(fact.fact_type)} evidence`}>
      <header className="mb-2 flex flex-wrap items-center gap-2">
        <h3 className="font-semibold">{humanize(fact.fact_type)}</h3>
        <span className="text-ink-muted">{humanize(fact.category)}</span>
        <StatusBadge status={fact.assertion_status} />
        <StatusBadge status={fact.validation_status} />
        <SyntheticBadge />
      </header>
      <dl className="mb-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-ink-muted">Subject</dt>
        <dd>{subject}</dd>
        <dt className="text-ink-muted">Timing</dt>
        <dd>{humanize(fact.temporal_status)}</dd>
        {original && (
          <>
            <dt className="text-ink-muted">Extracted value</dt>
            <dd lang={guessLang(original)} dir="auto">{original}</dd>
          </>
        )}
        {fact.review_reasons.length > 0 && (
          <>
            <dt className="text-ink-muted">Why it needs review</dt>
            <dd>{fact.review_reasons.map(humanize).join(", ")}</dd>
          </>
        )}
        {fact.uncertainty_note && (
          <>
            <dt className="text-ink-muted">Uncertainty</dt>
            <dd>{fact.uncertainty_note}</dd>
          </>
        )}
        {showResident && (
          <>
            <dt className="text-ink-muted">Resident</dt>
            <dd>
              <Link href={`/residents/${fact.resident_id}`} className="font-mono text-xs text-link underline underline-offset-2">
                {fact.resident_id.slice(0, 8)}
              </Link>
            </dd>
          </>
        )}
      </dl>
      {fact.sources.length > 0 ? (
        <div className="space-y-3">
          {fact.sources.map((s) => (
            <Source key={s.id} factId={fact.id} src={s} />
          ))}
        </div>
      ) : (
        <p className="text-warn">This fact has no stored source, so it cannot be verified.</p>
      )}
      <p className="mt-3 text-xs text-ink-muted">
        Extracted by {fact.model_provider ?? "unknown provider"} {fact.model_id ?? ""} (prompt {fact.prompt_version ?? "unknown"})
        {fact.extracted_at ? ` on ${fmtDateTime(fact.extracted_at)}` : ""}. Confidence reported by the model: {fact.extraction_confidence ?? "not given"}.
      </p>
      {onReview && <ReviewControls fact={fact} onDone={onReview} />}
    </article>
  );
}
