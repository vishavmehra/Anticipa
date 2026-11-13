"use client";

// Evidence extraction view: one resident's clinical note with every extracted fact highlighted at its stored offsets, the
// extracted evidence list, the guideline that matched and the care decision, and the record to action pipeline. One call
// per resident: GET /residents/{id}/journey (facts, documents, evaluations, cases) plus GET /evidence/{id}?context= for the
// text around the quotes (no document content endpoint exists, so the note shows up to 1,000 characters each side of the
// first quote). Real data only. Route: /evidence-review (first tab). Purpose: show judges how a note becomes a decision.

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, apiList } from "@/lib/api";
import { LANGUAGES, fmtDate, fmtInt, guessLang, humanize } from "@/lib/format";
import type { EvidenceDetail, EvidenceFact } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { StatusBadge } from "@/components/status-badge";
import { Card, CardHeader, DataLabel, EmptyState, ErrorPanel, Pill, inputClass, selectClass, type IconName, DuoIcon, Icon } from "@/components/ui";
import type { JourneyEvaluation, JourneyFact, JourneyPayload } from "./payloads";
import { dirFor, idWords, plain } from "./shared";
import { InfoTip } from "./interactive";

const CP = (s: string) => Array.from(s);

interface Span {
  start: number;
  end: number;
  n: number;
  tone: "risk" | "fact";
}

/** The note with numbered highlights. Offsets are Unicode code points into `text`; overlapping spans are skipped. */
function MarkedNote({ text, spans, lang, sel, visible, onSelect }: { text: string; spans: Span[]; lang: string | null; sel: number | null; visible: Set<number>; onSelect: (n: number) => void }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (sel == null) return;
    const m = box.current?.querySelector<HTMLElement>(`mark[data-fact-n="${sel}"]`);
    if (m && box.current) box.current.scrollTop = Math.max(0, m.offsetTop - box.current.clientHeight / 3);
  }, [sel]);
  const cps = CP(text);
  const ok = spans.filter((s) => s.start >= 0 && s.end <= cps.length && s.start < s.end).sort((a, b) => a.start - b.start);
  const parts: { t: string; span?: Span }[] = [];
  let at = 0;
  for (const s of ok) {
    if (s.start < at) continue;
    if (s.start > at) parts.push({ t: cps.slice(at, s.start).join("") });
    parts.push({ t: cps.slice(s.start, s.end).join(""), span: s });
    at = s.end;
  }
  if (at < cps.length) parts.push({ t: cps.slice(at).join("") });
  return (
    <div ref={box} lang={lang ?? undefined} dir={dirFor(lang)} role="region" aria-label="Clinical note with extracted facts highlighted" tabIndex={0} className="max-h-80 overflow-y-auto rounded-card border border-line bg-surface px-5 py-4 leading-8 whitespace-pre-wrap shadow-card">
      {parts.map((p, i) =>
        p.span ? (
          <mark key={i} data-fact-n={p.span.n} data-selected={sel === p.span.n} tabIndex={0} role="button" aria-pressed={sel === p.span.n} onClick={() => onSelect(p.span!.n)} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onSelect(p.span!.n))} className={`cursor-pointer rounded-md px-1.5 py-0.5 text-ink transition-opacity ${!visible.has(p.span.n) ? "bg-transparent opacity-50 underline decoration-dotted" : p.span.tone === "risk" ? "bg-[#e4dafb] shadow-[inset_0_-2px_0_#7b5bd6]" : "bg-mark shadow-[inset_0_-2px_0_var(--color-ink)]"} ${sel === p.span.n ? "outline-2 outline-offset-2 outline-brand" : ""}`}>
            {p.t}
            <sup className="ml-0.5 text-[0.65rem] font-bold text-brand-ink">{p.span.n}</sup>
          </mark>
        ) : (
          <span key={i}>{p.t}</span>
        ),
      )}
    </div>
  );
}

function badgeFor(f: JourneyFact): { label: string; tone: "ok" | "brand" | "warn" | "neutral" } {
  if (f.assertion_status === "negative") return { label: "Negated", tone: "neutral" };
  if (f.assertion_status === "uncertain" || f.assertion_status === "conditional") return { label: "Uncertain", tone: "warn" };
  if (f.category === "family_history") return { label: "Risk factor", tone: "brand" };
  return { label: "Fact", tone: "ok" };
}

const factValue = (f: JourneyFact): string => {
  const v = f.value ?? {};
  const s = (k: string) => (typeof v[k] === "string" ? (v[k] as string) : null);
  return s("original_text") ?? s("concept") ?? s("display") ?? humanize(f.fact_type);
};

function pickEvaluation(j: JourneyPayload): JourneyEvaluation | null {
  const items = j.evaluations?.items.filter((e) => e.is_latest) ?? [];
  const fromCase = j.cases?.items[0]?.evaluation_id;
  return items.find((e) => e.id === fromCase) ?? items.find((e) => e.outcome_class === "eligible") ?? items.find((e) => e.outcome === "requires_clinical_review") ?? items[0] ?? null;
}

interface Crit {
  criterion_id: string;
  outcome?: string;
  detail?: string;
}

function NoteCard({ j, residentId, idx, setIdx, sel, visible, onSelect }: { j: JourneyPayload; residentId: string; idx: number; setIdx: (n: number) => void; sel: number | null; visible: Set<number>; onSelect: (n: number) => void }) {
  const docs = j.evidence?.documents ?? [];
  const doc = docs[Math.min(idx, Math.max(0, docs.length - 1))];
  const facts = useMemo(() => (j.evidence?.facts ?? []).filter((f) => f.sources.some((s) => s.document_id === doc?.id)), [j, doc]);
  const first = facts.find((f) => f.sources[0]?.start_offset != null);
  const ctx = useApi(`note-ctx:${first?.id ?? "none"}`, (signal): Promise<{ data: EvidenceDetail } | null> => (first ? api<EvidenceDetail>(`/evidence/${first.id}`, { query: { context: 1000 }, signal }) : Promise.resolve(null)));
  const c = ctx.data?.data.context?.find((x) => x.source_id === first?.sources[0]?.id);
  const text = c ? c.before + c.quote + c.after : "";
  const excerptStart = c && first?.sources[0]?.start_offset != null ? first.sources[0].start_offset - CP(c.before).length : 0;
  const spans: Span[] = facts.flatMap((f, i) => {
    const s = f.sources.find((x) => x.document_id === doc?.id);
    return s?.start_offset != null && s.end_offset != null ? [{ start: s.start_offset - excerptStart, end: s.end_offset - excerptStart, n: i + 1, tone: f.category === "family_history" ? ("risk" as const) : ("fact" as const) }] : [];
  });
  const lang = doc?.language ?? guessLang(text) ?? null;

  return (
    <Card as="section" aria-labelledby="note-h" className="glass-strong">
      <CardHeader
        id="note-h"
        icon="file"
        title="Clinical note"
        subtitle={<>Resident <span className="font-mono text-xs">{residentId.slice(0, 8)}</span>{doc ? `. ${humanize(doc.document_type)}${doc.created_on ? `, ${fmtDate(doc.created_on)}` : ""}` : ""}</>}
        aside={
          docs.length > 1 ? (
            <div className="flex items-center gap-1" role="group" aria-label="Notes of this resident">
              <button type="button" aria-label="Previous note" disabled={idx === 0} onClick={() => setIdx(idx - 1)} className="grid size-9 place-items-center rounded-full border border-line bg-surface disabled:opacity-40">
                <Icon name="chevron-right" className="size-4 rotate-180" />
              </button>
              <span className="tnum min-w-12 text-center font-semibold">{idx + 1} / {docs.length}</span>
              <button type="button" aria-label="Next note" disabled={idx >= docs.length - 1} onClick={() => setIdx(idx + 1)} className="grid size-9 place-items-center rounded-full border border-line bg-surface disabled:opacity-40">
                <Icon name="chevron-right" className="size-4" />
              </button>
            </div>
          ) : undefined
        }
      />
      <div className="space-y-3 px-5 pb-5">
        {!doc ? (
          <p className="text-ink-muted">No note is registered for this resident.</p>
        ) : facts.length === 0 ? (
          <p className="rounded-control border border-dashed border-line-strong px-4 py-4 text-ink-muted">This note has not been read by the extraction job yet, so no facts are highlighted.</p>
        ) : ctx.error ? (
          <ErrorPanel error={ctx.error} onRetry={ctx.reload} title="Could not load the note text" />
        ) : !c ? (
          <div className="skeleton h-48" role="status" aria-label="Loading the note" />
        ) : (
          <>
            <MarkedNote text={text} spans={spans} lang={lang} sel={sel} visible={visible} onSelect={onSelect} />
            <p className="text-xs text-ink-muted">
              Showing the text around the extracted quotes{doc.language ? `, written in ${LANGUAGES[doc.language] ?? doc.language}` : ""}. Numbers match the evidence list. Quotes outside this excerpt are listed but not highlighted.
            </p>
          </>
        )}
      </div>
    </Card>
  );
}

type FactFilter = "all" | "risk" | "negated" | "review";
const FILTERS: { key: FactFilter; label: string }[] = [
  { key: "all", label: "All facts" },
  { key: "risk", label: "Risk factors" },
  { key: "negated", label: "Negated" },
  { key: "review", label: "Needs review" },
];
const matches = (f: JourneyFact, k: FactFilter) => k === "all" || (k === "risk" && f.category === "family_history" && f.assertion_status !== "negative") || (k === "negated" && f.assertion_status === "negative") || (k === "review" && f.validation_status === "requires_review");

function EvidenceList({ facts, filter, setFilter, sel, onSelect }: { facts: JourneyFact[]; filter: FactFilter; setFilter: (k: FactFilter) => void; sel: number | null; onSelect: (n: number) => void }) {
  const [all, setAll] = useState(false);
  const rows = facts.map((f, i) => ({ f, n: i + 1 })).filter((r) => matches(r.f, filter));
  const shown = all ? rows : rows.slice(0, 6);
  return (
    <Card as="section" aria-labelledby="ev-h" className="glass-strong">
      <CardHeader id="ev-h" icon="sparkles" title={<>Extracted evidence <InfoTip text="Each fact was read by AI from the note and carries the exact quote it came from. The quote is checked against the note by plain text matching, never by AI." /></>} subtitle={`${fmtInt(facts.length)} facts from this note. Select one to find it in the note.`} aside={<DataLabel kind="live" />} />
      <div className="px-5 pb-5">
        <div role="group" aria-label="Filter facts" className="mb-3 flex flex-wrap gap-1.5">
          {FILTERS.map((x) => (
            <button key={x.key} type="button" aria-pressed={filter === x.key} onClick={() => setFilter(x.key)} className={`min-h-8 rounded-full border px-3 text-[0.8125rem] font-semibold ${filter === x.key ? "border-brand bg-brand-soft text-brand-ink" : "border-line bg-surface text-ink-soft hover:bg-canvas"}`}>
              {x.label} <span className="tnum text-ink-muted">{facts.filter((f) => matches(f, x.key)).length}</span>
            </button>
          ))}
        </div>
        {facts.length === 0 ? (
          <p className="text-ink-muted">No facts were extracted from this note.</p>
        ) : rows.length === 0 ? (
          <p className="rounded-control border border-dashed border-line-strong px-4 py-4 text-ink-muted">No fact in this note matches the filter.</p>
        ) : (
          <ol aria-label="Extracted facts" className="space-y-2">
            {shown.map(({ f, n }) => {
              const b = badgeFor(f);
              const src = f.sources[0];
              return (
                <li key={f.id} data-fact={f.id}>
                  <button type="button" aria-pressed={sel === n} onClick={() => onSelect(n)} className={`flex w-full items-start gap-3 rounded-card border px-3 py-3 text-left shadow-card transition-colors ${sel === n ? "border-brand bg-brand-soft" : "border-line bg-surface hover:bg-canvas"}`}>
                    <span className="grid size-9 shrink-0 place-items-center rounded-full bg-brand-soft text-brand-ink">
                      <DuoIcon name={f.category === "family_history" ? "population" : /cancer/.test(f.fact_type) ? "ribbon" : "note"} className="size-5" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block font-semibold">
                        <span className="mr-1.5 tnum text-xs text-brand-ink">{n}</span>
                        {humanize(f.fact_type)}
                        {f.subject_type === "family_member" && f.relationship_type ? <span className="font-normal text-ink-muted">, {humanize(f.relationship_type)}</span> : null}
                      </span>
                      <span dir="auto" className="line-clamp-2 block text-[0.8125rem] text-ink-muted">{factValue(f)}</span>
                      <span className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        <Pill tone={b.tone}>{b.label}</Pill>
                        {src && <StatusBadge status={src.verification_status} />}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ol>
        )}
        {rows.length > 6 && (
          <button type="button" className="mt-3 text-brand-ink underline underline-offset-2" onClick={() => setAll((a) => !a)}>
            {all ? "Show fewer facts" : `Show all ${rows.length} facts`}
          </button>
        )}
      </div>
    </Card>
  );
}

function Matching({ j }: { j: JourneyPayload }) {
  const ev = j.evaluations ? pickEvaluation(j) : null;
  const criteria = ((ev?.explanation as { criteria?: Crit[] } | undefined)?.criteria ?? []).slice(0, 4);
  const matched = ev?.outcome_class === "eligible" || ev?.outcome === "requires_clinical_review";
  return (
    <>
      <Card as="section" aria-labelledby="gm-h" className="glass-strong">
        <CardHeader id="gm-h" icon="scale" title="Guideline matching" />
        <div className="space-y-3 px-5 pb-5">
          {j.evaluations === null ? (
            <p className="text-ink-muted">Your role cannot view guideline evaluations.</p>
          ) : !ev ? (
            <p className="text-ink-muted">No guideline has been evaluated for this resident yet.</p>
          ) : (
            <>
              <div className="flex items-center justify-between gap-2 rounded-card bg-brand-soft px-4 py-3">
                <div>
                  <p className="font-semibold">{ev.guideline?.name ? plain(ev.guideline.name) : humanize(ev.guideline?.care_activity)}</p>
                  <p className="text-[0.8125rem] text-ink-soft">
                    {ev.guideline?.key} version {ev.guideline?.version}
                    {ev.guideline?.review_status ? `. Rule status: ${humanize(ev.guideline.review_status)}` : ""}
                  </p>
                </div>
                <Pill tone={matched ? "ok" : "neutral"} icon={matched ? "check-circle" : "info"}>{matched ? "Matched" : "Not matched"}</Pill>
              </div>
              <ul aria-label="Matched criteria" className="space-y-2">
                {criteria.map((c) => {
                  const pass = c.outcome === "passed";
                  const fail = c.outcome === "failed";
                  return (
                    <li key={c.criterion_id} className="flex items-start gap-2">
                      <Icon name={pass ? "check-circle" : fail ? "x-circle" : "warning"} className={`mt-0.5 size-4 shrink-0 ${pass ? "text-ok" : fail ? "text-danger" : "text-warn"}`} />
                      <span>
                        <span className="font-medium">{idWords(c.criterion_id)}.</span> <span className="text-ink-muted">{plain(c.detail)}</span>
                      </span>
                    </li>
                  );
                })}
                {criteria.length === 0 && <li className="text-ink-muted">No criteria were stored for this evaluation.</li>}
              </ul>
            </>
          )}
        </div>
      </Card>
      <Card as="section" aria-labelledby="cd-h" className="glass-strong">
        <CardHeader id="cd-h" icon="target" title="Care decision" />
        <div className="px-5 pb-5">
          {ev ? (
            <div className={`flex items-start gap-3 rounded-card px-4 py-3 ${matched ? "bg-tint-green" : "bg-canvas"}`}>
              <Icon name={matched ? "check-circle" : "info"} className={`mt-0.5 size-6 shrink-0 ${matched ? "text-icon-green" : "text-ink-muted"}`} />
              <div>
                <p className="text-base font-semibold">{matched ? `${humanize(ev.guideline?.care_activity)} recommended` : `${humanize(ev.guideline?.care_activity)}: ${humanize(ev.outcome)}`}</p>
                <p className="text-ink-soft">{plain((ev.explanation as { decision?: { explanation?: string | null } }).decision?.explanation) || (matched ? "Eligible under the guideline." : "The rule did not find the resident due.")}</p>
                <p className="mt-1 text-xs text-ink-muted">Decided by a versioned rule. AI reads the notes and does not decide.</p>
              </div>
            </div>
          ) : (
            <p className="text-ink-muted">No decision yet.</p>
          )}
        </div>
      </Card>
    </>
  );
}

function Pipeline({ j }: { j: JourneyPayload }) {
  const steps: { icon: IconName; title: string; sub: string }[] = [
    { icon: "file", title: "Clinical record", sub: `${fmtInt(j.resident.counts.documents)} unstructured notes` },
    { icon: "sparkles", title: "Extract evidence", sub: `${fmtInt(j.resident.counts.evidence_facts)} structured, traceable facts` },
    { icon: "scale", title: "Apply guidelines", sub: `${fmtInt(j.evaluations?.items.filter((e) => e.is_latest).length ?? 0)} deterministic evaluations` },
    { icon: "target", title: "Care action", sub: `${fmtInt(j.cases?.items.length ?? 0)} care gap cases` },
  ];
  return (
    <Card as="section" aria-labelledby="pipe-h" className="glass-strong">
      <div className="flex flex-wrap items-center gap-x-8 gap-y-4 px-5 py-4">
        <div className="min-w-48">
          <h2 id="pipe-h" className="text-base font-semibold">From record to action</h2>
          <p className="text-[0.8125rem] text-ink-muted">The Anticipa evidence pipeline for this resident.</p>
        </div>
        <ol className="flex flex-1 flex-wrap items-center justify-between gap-4">
          {steps.map((s, i) => (
            <li key={s.title} className="flex items-center gap-4">
              <div className="text-center">
                <span className="mx-auto grid size-12 place-items-center rounded-2xl bg-brand-soft text-brand-ink">
                  <Icon name={s.icon} className="size-6" />
                </span>
                <p className="mt-1 font-semibold">{s.title}</p>
                <p className="text-xs text-ink-muted">{s.sub}</p>
              </div>
              {i < steps.length - 1 && <Icon name="arrow-right" className="hidden size-5 text-ink-muted sm:block" />}
            </li>
          ))}
        </ol>
      </div>
    </Card>
  );
}

function Body({ j, residentId }: { j: JourneyPayload; residentId: string }) {
  const [idx, setIdx] = useState(0);
  const [sel, setSel] = useState<number | null>(null);
  const [filter, setFilter] = useState<FactFilter>("all");
  const docs = j.evidence?.documents ?? [];
  const docId = docs[Math.min(idx, Math.max(0, docs.length - 1))]?.id;
  const facts = useMemo(() => (j.evidence?.facts ?? []).filter((f) => f.sources.some((s) => s.document_id === docId)), [j, docId]);
  const visible = useMemo(() => new Set(facts.flatMap((f, i) => (matches(f, filter) ? [i + 1] : []))), [facts, filter]);
  const move = (n: number) => {
    setIdx(n);
    setSel(null);
  };
  return (
    <>
      <div className="grid gap-5 xl:grid-cols-[1.1fr_1.05fr_0.9fr] *:min-w-0">
        <NoteCard j={j} residentId={residentId} idx={idx} setIdx={move} sel={sel} visible={visible} onSelect={setSel} />
        <EvidenceList facts={facts} filter={filter} setFilter={setFilter} sel={sel} onSelect={setSel} />
        <div className="space-y-5">
          <Matching j={j} />
        </div>
      </div>
      <Pipeline j={j} />
    </>
  );
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function EvidenceExtraction() {
  const [picked, setPicked] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const recent = useApi("evidence-recent", (s) => apiList<EvidenceFact>("/evidence", { page_size: 40, validation_status: "accepted" }, s));
  const residents = useMemo(() => [...new Set((recent.data?.data ?? []).map((f) => f.resident_id))].slice(0, 12), [recent.data]);
  const residentId = picked ?? residents[0] ?? null;
  const journey = useApi(`evidence-journey:${residentId}`, (s): Promise<{ data: JourneyPayload } | null> => (residentId ? api<JourneyPayload>(`/residents/${residentId}/journey`, { signal: s }) : Promise.resolve(null)));
  const j = journey.data?.data;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label htmlFor="ev-res" className="mb-1 block font-medium">Resident with extracted evidence</label>
          <select id="ev-res" value={residentId ?? ""} onChange={(e) => setPicked(e.target.value)} className={`${selectClass} min-w-64`} disabled={residents.length === 0}>
            {residents.length === 0 && <option value="">None yet</option>}
            {residents.map((r) => (
              <option key={r} value={r}>
                {r.slice(0, 8)}
              </option>
            ))}
          </select>
        </div>
        <form
          className="flex items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (UUID.test(typed.trim())) setPicked(typed.trim());
          }}
        >
          <div>
            <label htmlFor="ev-id" className="mb-1 block font-medium">Or open a resident by id</label>
            <input id="ev-id" value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="Full resident id" className={`${inputClass} w-80 font-mono text-xs`} aria-invalid={typed.length > 0 && !UUID.test(typed.trim())} />
          </div>
          <button type="submit" className="inline-flex min-h-9 items-center rounded-control border border-line-strong bg-surface px-3 font-semibold" disabled={!UUID.test(typed.trim())}>
            Open
          </button>
        </form>
        {residentId && (
          <Link href={`/residents/${residentId}/journey`} className="ml-auto inline-flex items-center gap-1 text-brand-ink underline underline-offset-2">
            Open the full journey <Icon name="arrow-right" className="size-4" />
          </Link>
        )}
      </div>

      {recent.error ? (
        <ErrorPanel error={recent.error} onRetry={recent.reload} title="Could not load the evidence list" />
      ) : !recent.data ? (
        <div className="skeleton h-72" role="status" aria-label="Loading evidence" />
      ) : !residentId ? (
        <EmptyState title="No evidence has been extracted yet" icon="sparkles">Facts appear here after the extraction job has read clinical notes.</EmptyState>
      ) : journey.error ? (
        <ErrorPanel error={journey.error} onRetry={journey.reload} title="Could not load this resident" />
      ) : !j ? (
        <div className="skeleton h-72" role="status" aria-label="Loading the resident" />
      ) : (
        <Body key={residentId} j={j} residentId={residentId} />
      )}
    </div>
  );
}
