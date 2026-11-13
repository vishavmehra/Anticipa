"use client";

// Evidence-in-note viewer: the original note with the extracted quote highlighted at its stored offsets, plus the
// attributes a reviewer needs (assertion, subject, timing, verification, model, prompt). Callers: journey READ panel,
// WhyDue evidence links. Reads GET /evidence/{id}?context=1000 (the API caps context at 1,000 characters each side;
// no document-content endpoint exists in this build). Shows judges where every fact came from.

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { fmtDateTime, guessLang, humanize } from "@/lib/format";
import type { EvidenceDetail, EvidenceSource } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { StatusBadge } from "@/components/status-badge";
import { ErrorPanel, btnSecondary } from "@/components/ui";
import { Tag, dirFor, plain } from "./shared";

const CONTEXT = 1000; // backend maximum (evidence/routes.py: context le=1000)

/**
 * Note text with one highlighted span. `start`/`end` are Unicode code-point offsets into `text` (Python str indices),
 * end-exclusive. Highlight is background + underline with no padding or weight change, so Arabic joining survives.
 * Line breaks of the original note are kept. A span that does not match the stored quote is flagged, never hidden.
 */
export function NoteText({ text, start, end, quote, lang, label }: { text: string; start: number; end: number; quote?: string | null; lang?: string | null; label: string }) {
  const cps = useMemo(() => Array.from(text), [text]);
  const valid = Number.isInteger(start) && Number.isInteger(end) && start >= 0 && start < end && end <= cps.length;
  const hit = valid ? cps.slice(start, end).join("") : "";
  const mismatch = valid && quote != null && hit.normalize("NFC") !== quote.normalize("NFC");
  const markRef = useRef<HTMLElement>(null);
  useEffect(() => {
    // Keep the highlight in view inside the scroll box without moving the page.
    const m = markRef.current;
    const box = m?.closest("[data-note-scroll]") as HTMLElement | null;
    if (m && box) box.scrollTop = Math.max(0, m.offsetTop - box.clientHeight / 3);
  }, [text, start, end]);

  return (
    <figure className="overflow-hidden rounded-control border border-line bg-surface">
      <div
        data-note-scroll
        role="region"
        aria-label={label}
        tabIndex={0}
        lang={lang ?? undefined}
        dir={dirFor(lang)}
        className="relative max-h-72 overflow-y-auto px-4 py-3 leading-7 whitespace-pre-wrap text-ink"
      >
        {valid ? (
          <>
            {cps.slice(0, start).join("")}
            <mark ref={markRef} className="bg-mark text-ink shadow-[inset_0_-2px_0_var(--color-ink)]">
              {hit}
            </mark>
            {cps.slice(end).join("")}
          </>
        ) : (
          text
        )}
      </div>
      <figcaption className="tnum flex flex-wrap items-center gap-x-3 gap-y-0.5 border-t border-line px-3 py-1.5 text-xs text-ink-muted">
        {!valid ? (
          <span className="font-medium text-warn">The stored position is outside this text, so the quote cannot be found here.</span>
        ) : mismatch ? (
          <span className="font-medium text-danger">The text at this position does not match the stored quote. It is not verified.</span>
        ) : (
          <span className="font-medium text-ok">The highlighted text matches the stored quote exactly.</span>
        )}
      </figcaption>
    </figure>
  );
}

const isVerified = (s: EvidenceSource) => s.verification_status.startsWith("verified_");

export function EvidenceInNote({ factId, onClose }: { factId: string; onClose?: () => void }) {
  const res = useApi(`note:${factId}`, (signal) => api<EvidenceDetail>(`/evidence/${factId}`, { query: { context: CONTEXT }, signal }));
  const [pick, setPick] = useState(0);

  if (res.error) return <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load this evidence" />;
  if (!res.data) {
    return (
      <p role="status" className="text-ink-muted">
        Loading the note.
      </p>
    );
  }
  const f = res.data.data;
  const src = f.sources[Math.min(pick, f.sources.length - 1)];
  const ctx = src ? (f.context ?? []).find((c) => c.source_id === src.id) : undefined;
  const text = ctx ? ctx.before + ctx.quote + ctx.after : "";
  const start = ctx ? Array.from(ctx.before).length : 0;
  const end = ctx ? start + Array.from(ctx.quote).length : 0;
  const lang = f.document?.language ?? guessLang(text) ?? guessLang(src?.quote ?? "") ?? null;
  const subject = f.subject_type === "family_member" ? `Relative: ${humanize(f.relationship_type)}` : f.subject_type === "resident" ? "The resident" : humanize(f.subject_type);
  const truncated = ctx ? Array.from(ctx.before).length >= CONTEXT || Array.from(ctx.after).length >= CONTEXT : false;
  const noteStart = ctx && src?.start_offset != null ? src.start_offset - start : null;

  return (
    <div className="space-y-3" data-testid="note-viewer">
      <div className="flex flex-wrap items-center gap-1.5">
        <StatusBadge status={f.assertion_status} />
        <Tag title="Whose condition the statement is about">{subject}</Tag>
        <Tag title="When the statement says it applies">Timing: {humanize(f.temporal_status)}</Tag>
        {src && <StatusBadge status={src.verification_status} />}
        <StatusBadge status={f.validation_status} />
        <Tag>{humanize(f.fact_type)}</Tag>
        {f.document?.language && <Tag title="Language of the source note">{f.document.language.toUpperCase()}</Tag>}
      </div>

      {f.sources.length > 1 && (
        <div role="group" aria-label="Source quotes for this fact" className="flex flex-wrap gap-1.5">
          {f.sources.map((s, i) => (
            <button key={s.id} type="button" aria-pressed={i === pick} onClick={() => setPick(i)} className={`${btnSecondary} ${i === pick ? "bg-neutral-bg" : ""}`}>
              Source {i + 1}
            </button>
          ))}
        </div>
      )}

      {!src ? (
        <p className="text-warn">This fact has no stored source, so there is no note to show.</p>
      ) : !ctx ? (
        <>
          <p className="text-ink-muted">The text around the quote is not available. Only the stored quote is shown.</p>
          {src.quote && (
            <blockquote lang={lang ?? undefined} dir={dirFor(lang)} className="rounded-control border border-line bg-canvas px-3 py-2 leading-7">
              {src.quote}
            </blockquote>
          )}
        </>
      ) : (
        <>
          <NoteText text={text} start={start} end={end} quote={src.quote} lang={lang} label={`Source note${f.document ? `, ${humanize(f.document.document_type)}` : ""}, with the extracted quote highlighted`} />
          <p className="text-xs text-ink-muted">
            {truncated ? `Showing up to ${CONTEXT.toLocaleString("en-US")} characters on each side of the quote. The whole note is not available here. ` : "This is the whole note around the quote. "}
            {noteStart != null && src.start_offset != null && src.end_offset != null ? `The quote runs from character ${src.start_offset.toLocaleString("en-US")} to ${src.end_offset.toLocaleString("en-US")} of the note` : ""}
            {noteStart != null && noteStart > 0 ? `. This excerpt starts at character ${noteStart.toLocaleString("en-US")}.` : noteStart != null ? "." : ""}
          </p>
        </>
      )}

      {src && !isVerified(src) && <p className="text-warn">This quote could not be checked against its source text. The fact cannot be accepted.</p>}
      {src?.location_ambiguous && <p className="text-xs text-warn">The quote appears more than once in the note. The highlight marks the place the model pointed to.</p>}

      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-ink-muted">Extracted by</dt>
        <dd>
          {f.model_id ? <><span>{f.model_provider} </span><span className="font-mono text-xs">{f.model_id}</span></> : "Unknown model"}
          {f.extracted_at ? <span className="text-ink-muted">, {fmtDateTime(f.extracted_at)}</span> : null}
        </dd>
        <dt className="text-ink-muted">Prompt version</dt>
        <dd className="font-mono text-xs">{f.prompt_version ?? "Not recorded"}</dd>
        {f.invocation && (
          <>
            <dt className="text-ink-muted">Model call</dt>
            <dd>
              {[f.invocation.task, f.invocation.latency_ms != null ? `${Math.round(f.invocation.latency_ms)} ms` : null, f.invocation.cost_usd != null ? `$${f.invocation.cost_usd.toFixed(4)} estimated cost` : null].filter(Boolean).join(", ")}
            </dd>
          </>
        )}
        <dt className="text-ink-muted">Confidence given by the model</dt>
        <dd>{f.extraction_confidence ? humanize(f.extraction_confidence) : "Not given"}</dd>
        {f.uncertainty_note && (
          <>
            <dt className="text-ink-muted">Uncertainty note</dt>
            <dd>{plain(f.uncertainty_note)}</dd>
          </>
        )}
        {f.review_reasons.length > 0 && (
          <>
            <dt className="text-ink-muted">Why it needs review</dt>
            <dd>{f.review_reasons.map(humanize).join(", ")}</dd>
          </>
        )}
        {f.review && (
          <>
            <dt className="text-ink-muted">Reviewer decision</dt>
            <dd>
              <StatusBadge status={f.review.decision} /> <span className="text-ink-muted">{fmtDateTime(f.review.reviewed_at)}</span>
              {f.review.note ? <span className="block">{plain(f.review.note)}</span> : null}
            </dd>
          </>
        )}
      </dl>

      {onClose && (
        <button type="button" className={btnSecondary} onClick={onClose}>
          Close the note
        </button>
      )}
    </div>
  );
}

/** A button that reveals the viewer under it. Used wherever a fact or evidence id is listed. */
export function ShowInNote({ factId, label = "Show quote in note" }: { factId: string; label?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-1">
      <button type="button" className="text-brand-ink underline underline-offset-2" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {open ? "Hide the note" : label}
      </button>
      {open && (
        <div className="mt-2 rounded-control border border-line bg-canvas p-4">
          <EvidenceInNote factId={factId} />
        </div>
      )}
    </div>
  );
}
