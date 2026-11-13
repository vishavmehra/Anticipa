"use client";

// "Why is this person due?": criterion-by-criterion checklist from the stored eligibility evaluation (never re-derived
// in the browser and never from an AI model), with rule + guideline id/version, source citation, evidence links and the
// the rule status exactly as the API states it. Callers: journey DECIDE panel (evaluation from the journey payload), care-gap detail
// (WhyDue by evaluation id -> GET /evaluations/{id}). Explanation shape: backend/anticipa/guidelines/evaluator.py
// GuidelineResult.explanation. Purpose: show judges why each decision was made.

import Link from "next/link";
import { api } from "@/lib/api";
import { fmtDate, fmtDateTime, humanize } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { StatusBadge } from "@/components/status-badge";
import { ErrorPanel, Pill } from "@/components/ui";
import { ShowInNote } from "./note-viewer";
import type { JourneyEvaluation } from "./payloads";
import { Tag, idWords, plain } from "./shared";
import type { EvaluationRecord } from "./types";

// ---- normalised input: the two endpoints name a few fields differently ----
export interface WhyDueInput {
  outcome: string;
  outcome_class: string;
  reason_code: string | null;
  care_activity: string | null;
  /** Status of the guideline version exactly as the API gives it (draft, in_review, approved, retired); null when not sent. */
  rule_status?: string | null;
  guideline: { id?: string | null; key?: string | null; version?: number | null; name?: string | null };
  decisive_rule: { id?: string | null; version?: number | null };
  evaluation_date: string;
  evaluated_at: string;
  execution_version?: string | null;
  explanation: Record<string, unknown>;
  evidence_references: unknown[];
  missing_information: unknown[];
  conflicting_information: unknown[];
  source_references: unknown[];
}

export const fromEvaluationRecord = (e: EvaluationRecord): WhyDueInput => ({
  outcome: e.outcome,
  outcome_class: e.outcome_class,
  reason_code: e.reason_code,
  care_activity: e.care_activity ?? null,
  guideline: { id: e.guideline_id, key: e.guideline_key, version: e.guideline_version },
  decisive_rule: { id: e.rule_id, version: e.rule_version },
  evaluation_date: e.evaluation_date,
  evaluated_at: e.evaluated_at,
  execution_version: e.execution_version,
  explanation: e.explanation,
  evidence_references: e.evidence_references,
  missing_information: e.missing_information,
  conflicting_information: e.conflicting_information,
  source_references: e.source_references,
});

export const fromJourneyEvaluation = (e: JourneyEvaluation): WhyDueInput => ({
  outcome: e.outcome,
  outcome_class: e.outcome_class,
  reason_code: e.reason_code,
  care_activity: e.guideline?.care_activity ?? null,
  rule_status: e.guideline?.review_status ?? null,
  guideline: { id: e.guideline?.id, key: e.guideline?.key, version: e.guideline?.version, name: e.guideline?.name },
  decisive_rule: { id: e.decisive_rule?.id, version: e.decisive_rule?.version },
  evaluation_date: e.evaluation_date,
  evaluated_at: e.evaluated_at,
  execution_version: e.execution_version,
  explanation: e.explanation,
  evidence_references: e.evidence_references,
  missing_information: e.missing_information,
  conflicting_information: e.conflicting_information,
  source_references: e.source_references,
});

// ---- explanation parsing (all keys optional: an older evaluation may carry fewer) ----
interface Criterion {
  criterion_id: string;
  rule_type?: string;
  priority?: number;
  result?: boolean | "unknown";
  outcome?: string; // passed | failed | unknown (Docs/08 §7.1)
  detail?: string;
  contributes?: string;
  reason_code?: string;
  review_required?: boolean;
}
interface Explanation {
  decision?: { reason_code?: string | null; decisive_rule_id?: string | null; explanation?: string | null };
  criteria?: Criterion[];
  decisive_rule?: { rule_id?: string; rule_version?: number };
}
interface EvidenceRef {
  type?: string;
  id?: string;
  source_reference?: string;
  document_id?: string;
  quote?: string;
}
interface SourceRefs {
  rule_id?: string;
  references?: { source_id?: string; title?: string; url?: string; publication_date?: string; citation?: string; grade_or_strength?: string }[];
  metadata?: { source_status?: unknown; assumption?: unknown; source_metadata?: Record<string, unknown> };
}

export type CriterionState = "pass" | "fail" | "missing" | "conflicting";

/** unknown + the engine's own "conflicting statements" wording = conflicting; other unknowns are missing information. */
export function criterionState(c: Criterion): CriterionState {
  // `outcome` is the criterion outcome after the rule's own semantics (an exclusion that is true "fails"); `result` is the raw value.
  if (c.outcome === "passed") return "pass";
  if (c.outcome === "failed") return "fail";
  if (c.outcome === undefined && c.result === true) return "pass";
  if (c.outcome === undefined && c.result === false) return "fail";
  return /conflict/i.test(c.detail ?? "") ? "conflicting" : "missing";
}

const STATE: Record<CriterionState, { label: string; tone: "ok" | "danger" | "warn"; icon: "check-circle" | "x-circle" | "warning" | "alert" }> = {
  pass: { label: "Pass", tone: "ok", icon: "check-circle" },
  fail: { label: "Fail", tone: "danger", icon: "x-circle" },
  missing: { label: "Missing", tone: "warn", icon: "warning" },
  conflicting: { label: "Conflicting", tone: "warn", icon: "alert" },
};

const arr = <T,>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);
const text = (v: unknown): string | null => (typeof v === "string" && v ? v : typeof v === "number" || typeof v === "boolean" ? String(v) : null);

function Citation({ s }: { s: SourceRefs }) {
  const meta = s.metadata;
  const status = text(meta?.source_status) ?? text(meta?.source_metadata?.["status"]);
  const refs = s.references ?? [];
  return (
    <li className="rounded-control border border-line bg-canvas px-3.5 py-2.5">
      <p className="font-medium">
        Rule <span className="font-mono text-xs">{s.rule_id}</span>
      </p>
      {refs.length === 0 && <p className="text-ink-muted">No source reference is stored for this rule.</p>}
      {refs.map((r, i) => (
        <div key={`${r.source_id}-${i}`} className="mt-1.5">
          <p>{r.citation || r.title ? plain(r.citation ?? r.title) : <span className="font-mono text-xs break-all">{r.source_id ?? "Source"}</span>}</p>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 text-xs text-ink-muted">
            <dt>Published</dt>
            <dd>{r.publication_date ? fmtDate(r.publication_date) : "Not stated"}</dd>
            <dt>Evidence grade</dt>
            <dd>{r.grade_or_strength ? plain(r.grade_or_strength) : "Not stated"}</dd>
            {r.url && (
              <>
                <dt>Link</dt>
                <dd className="font-mono text-xs break-all">{r.url}</dd>
              </>
            )}
          </dl>
        </div>
      ))}
      <p className="mt-1.5 text-xs text-ink-muted">
        Source status: <span className="font-medium text-ink">{status ? humanize(status) : "Not stated"}</span>
        {text(meta?.assumption) ? <span className="block">Assumption: {plain(text(meta?.assumption))}</span> : null}
      </p>
    </li>
  );
}

const H = "mb-1.5 text-sm font-semibold";

/** The explainer body for one evaluation (already loaded). */
export function WhyDueBody({ ev }: { ev: WhyDueInput }) {
  const x = (ev.explanation ?? {}) as Explanation;
  const criteria = arr<Criterion>(x.criteria);
  const evidence = arr<EvidenceRef>(ev.evidence_references);
  const sources = arr<SourceRefs>(ev.source_references);
  const conflicts = arr<{ type?: string; subject?: string; values?: unknown[] }>(ev.conflicting_information);
  const missing = arr<{ criterion_id?: string; reason_code?: string; detail?: string }>(ev.missing_information);
  const decisive = x.decision?.decisive_rule_id ?? x.decisive_rule?.rule_id ?? ev.decisive_rule.id;
  const due = ev.outcome_class === "eligible" || ev.outcome === "requires_clinical_review";

  return (
    <div className="space-y-5" data-testid="why-due">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={ev.outcome} />
        {ev.rule_status ? <Pill tone={ev.rule_status === "approved" ? "ok" : "neutral"}>Rule status: {humanize(ev.rule_status)}</Pill> : null}
      </div>
      <p className="max-w-[72ch] text-ink-soft">
        {due ? "Due for " : "Outcome for "}
        <span className="font-semibold text-ink">{humanize(ev.care_activity)}</span>
        {x.decision?.explanation ? `. ${plain(x.decision.explanation)}` : "."}
      </p>

      <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 rounded-control border border-line bg-canvas px-4 py-3">
        <dt className="text-ink-muted">Guideline</dt>
        <dd>
          {ev.guideline.name ? `${plain(ev.guideline.name)} ` : ""}
          <span className="font-mono text-xs">{ev.guideline.key ?? "unknown"}</span>
          {ev.guideline.version != null ? `, version ${ev.guideline.version}` : ""}
          {ev.guideline.id ? (
            <>
              {" "}
              <Link href={`/guidelines/${ev.guideline.id}`} className="text-brand-ink underline underline-offset-2">
                Open guideline
              </Link>
            </>
          ) : null}
        </dd>
        <dt className="text-ink-muted">Deciding rule</dt>
        <dd>
          {decisive ? (
            <>
              <span className="font-mono text-xs">{decisive}</span>
              {ev.decisive_rule.version != null ? `, version ${ev.decisive_rule.version}` : ""}
            </>
          ) : due ? (
            "None. Every criterion was met."
          ) : (
            "Not recorded"
          )}
        </dd>
        <dt className="text-ink-muted">Reason code</dt>
        <dd className="font-mono text-xs">{ev.reason_code ?? x.decision?.reason_code ?? "None"}</dd>
        <dt className="text-ink-muted">Evaluated</dt>
        <dd>
          For {fmtDate(ev.evaluation_date)}. Run on {fmtDateTime(ev.evaluated_at)}
          {ev.execution_version ? <>. Engine <span className="font-mono text-xs">{ev.execution_version}</span></> : ""}
        </dd>
        <dt className="text-ink-muted">Decided by</dt>
        <dd>A versioned rule. AI reads the notes. It does not decide who is due.</dd>
      </dl>

      <section aria-label="Criteria checklist">
        <h4 className={H}>Criteria, one by one</h4>
        {criteria.length === 0 ? (
          <p className="text-ink-muted">This evaluation has no stored criteria.</p>
        ) : (
          <ol className="divide-y divide-line overflow-hidden rounded-control border border-line bg-surface">
            {criteria.map((c) => {
              const st = criterionState(c);
              return (
                <li key={c.criterion_id} data-criterion-state={st} className="grid gap-x-3 gap-y-1 px-4 py-2.5 sm:grid-cols-[7rem_1fr]">
                  <div>
                    <Pill tone={STATE[st].tone} icon={STATE[st].icon}>
                      {STATE[st].label}
                    </Pill>
                  </div>
                  <div className="min-w-0">
                    <p className="font-medium">
                      {idWords(c.criterion_id)}
                      <span className="ml-2 text-xs font-normal text-ink-muted">
                        {c.rule_type ? `${humanize(c.rule_type)} criterion` : "Criterion"}
                        {c.criterion_id === decisive ? ", decided the outcome" : ""}
                      </span>
                    </p>
                    {c.detail && <p className="text-ink-muted">{plain(c.detail)}</p>}
                    {(st === "missing" || st === "conflicting") && c.reason_code && (
                      <p className="text-xs text-warn">
                        Reason code {c.reason_code}
                        {c.review_required ? ". A clinician needs to review this." : ""}
                      </p>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        )}
        <p className="mt-1.5 text-xs text-ink-muted">Pass and Fail come from the rule. Missing means the information is not on file. Conflicting means records disagree.</p>
      </section>

      {conflicts.length > 0 && (
        <section aria-label="Conflicting information">
          <h4 className={H}>Conflicting statements</h4>
          <ul className="list-disc pl-5">
            {conflicts.map((c, i) => (
              <li key={i}>
                {humanize(c.type)}
                {c.subject ? ` about ${c.subject}` : ""}
                {c.values?.length ? `: ${c.values.join(" and ")}` : ""}
              </li>
            ))}
          </ul>
        </section>
      )}
      {missing.length > 0 && (
        <section aria-label="Missing information">
          <h4 className={H}>Information still needed</h4>
          <ul className="list-disc pl-5">
            {missing.map((m, i) => (
              <li key={i}>
                {idWords(m.criterion_id)}
                {m.detail ? `: ${plain(m.detail)}` : ""}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section aria-label="Evidence behind the decision">
        <h4 className={H}>Evidence behind the decision</h4>
        {evidence.length === 0 ? (
          <p className="text-ink-muted">No record or note is cited. The decision rests on age, sex or the lack of recorded history.</p>
        ) : (
          <ul className="space-y-2">
            {evidence.map((e, i) => (
              <li key={`${e.id}-${i}`} className="rounded-control border border-line bg-surface px-4 py-2.5">
                <p className="flex flex-wrap items-center gap-2">
                  <Tag>{e.type === "evidence_fact" ? "Fact read from a note" : e.type === "clinical_record" ? "Clinical record" : humanize(e.type)}</Tag>
                  {e.source_reference && <span className="font-mono text-xs break-all">{e.source_reference}</span>}
                </p>
                {e.quote && <blockquote className="mt-1.5 rounded-control bg-canvas px-3 py-2 text-ink-soft">{e.quote}</blockquote>}
                {e.type === "evidence_fact" && e.id && <ShowInNote factId={e.id} />}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Source citation">
        <h4 className={H}>Source citation</h4>
        {sources.length === 0 ? (
          <p className="text-ink-muted">No source citation is stored for this evaluation.</p>
        ) : (
          <ul className="space-y-2">
            {sources.map((s, i) => (
              <Citation key={`${s.rule_id}-${i}`} s={s} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

/** Fetches the stored evaluation by id and renders the explainer (used where only the id is known, e.g. the case page). */
export function WhyDue({ evaluationId }: { evaluationId: string }) {
  const res = useApi(`why-due:${evaluationId}`, (signal) => api<EvaluationRecord>(`/evaluations/${evaluationId}`, { signal }));
  if (res.error) return <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load the evaluation" />;
  if (!res.data)
    return (
      <p role="status" className="text-ink-muted">
        Loading the evaluation.
      </p>
    );
  return <WhyDueBody ev={fromEvaluationRecord(res.data.data)} />;
}
