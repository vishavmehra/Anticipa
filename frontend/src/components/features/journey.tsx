"use client";

// Resident journey: READ -> DECIDE -> PLAN -> ACT -> CLOSE for one resident, every stage showing the real records it
// produced and linking into the detail pages. Route: /residents/[id]/journey. One call: GET /residents/{id}/journey
// (backend/anticipa/analytics/journey.py; sections the caller may not see come back null). Nothing is invented: an
// empty or restricted stage says so. Purpose: show judges the whole loop on one screen.

import Link from "next/link";
import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { LANGUAGES, fmtDateTime, fmtInt, humanize } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { useCan } from "@/components/app-shell";
import { CaseConversation } from "@/components/conversation";
import { StatusBadge } from "@/components/status-badge";
import { Timeline, type TimelineItem } from "@/components/timeline";
import { Card, CardHeader, DataLabel, ErrorPanel, PageHeader, PriorityPill, btnSecondary, selectClass, GlassTile, type DuoName, Icon } from "@/components/ui";
import { OutreachPreview } from "./outreach-preview";
import { ShowInNote } from "./note-viewer";
import type { JourneyCase, JourneyPayload } from "./payloads";
import { FamilyPedigree } from "./pedigree";
import { Tag, dirFor } from "./shared";
import { STAGE_BLURB, STAGE_LABEL, type Stage } from "./types";
import { WhyDueBody, fromJourneyEvaluation } from "./why-due";

type StepState = "complete" | "current" | "none" | "ended" | "later" | "unavailable";
interface Step {
  stage: Stage;
  state: StepState;
  summary: string;
}

const TERMINAL = ["completed", "closed_not_completed", "cancelled"];
const AFTER_ACT = ["awaiting_completion_evidence", "completed"];
const noRole = "Your role cannot view this section";

/** Stage states from the real records only. READ and DECIDE are resident-level; PLAN, ACT, CLOSE follow the selected case. */
function deriveSteps(j: JourneyPayload, c: JourneyCase | undefined): Step[] {
  const facts = j.evidence?.facts ?? [];
  const evals = (j.evaluations?.items ?? []).filter((e) => e.is_latest);
  const due = evals.filter((e) => e.outcome_class === "eligible" || e.outcome === "requires_clinical_review").length;
  const docs = j.resident.counts.documents;
  const status = c?.status ?? null;
  const allocs = c?.allocations ?? [];
  const msgs = c?.communications ?? [];
  const appts = c?.appointments ?? [];
  const proofs = c?.completion_evidence ?? [];
  const verified = proofs.filter((p) => p.verification_status === "verified").length;

  const steps: Step[] = [
    j.evidence === null
      ? { stage: "read", state: "unavailable", summary: noRole }
      : facts.length > 0
        ? { stage: "read", state: "complete", summary: `${fmtInt(facts.length)} facts from ${j.evidence.documents.length} notes` }
        : { stage: "read", state: "none", summary: docs > 0 ? `${docs} notes registered, none read yet` : "No notes for this resident" },
    j.evaluations === null
      ? { stage: "decide", state: "unavailable", summary: noRole }
      : evals.length > 0
        ? { stage: "decide", state: "complete", summary: `${evals.length} guideline${evals.length === 1 ? "" : "s"} evaluated, ${due} due` }
        : { stage: "decide", state: "none", summary: "No guideline evaluation yet" },
    !c
      ? { stage: "plan", state: "later", summary: "No case to plan" }
      : c.allocations === null
        ? { stage: "plan", state: "unavailable", summary: noRole }
        : allocs.length > 0
          ? { stage: "plan", state: "complete", summary: `${humanize(allocs[0].allocation_status)} slot` }
          : status === "awaiting_capacity"
            ? { stage: "plan", state: "current", summary: "Waiting for capacity" }
            : { stage: "plan", state: "later", summary: "Not planned yet" },
    !c
      ? { stage: "act", state: "later", summary: "No outreach" }
      : c.communications === null && c.appointments === null
        ? { stage: "act", state: "unavailable", summary: noRole }
        : msgs.length > 0 || appts.length > 0
          ? { stage: "act", state: status && ["outreach_completed", "appointment_scheduled", ...AFTER_ACT].includes(status) ? "complete" : "current", summary: `${msgs.length} ${msgs.length === 1 ? "message" : "messages"}, ${appts.length} ${appts.length === 1 ? "appointment" : "appointments"}` }
          : { stage: "act", state: "later", summary: "No outreach yet" },
    !c
      ? { stage: "close", state: "later", summary: "Not closed" }
      : status === "completed"
        ? { stage: "close", state: "complete", summary: `Closed with ${verified} verified proof` }
        : status === "closed_not_completed" || status === "cancelled"
          ? { stage: "close", state: "ended", summary: humanize(status) }
          : status === "awaiting_completion_evidence"
            ? { stage: "close", state: "current", summary: "Awaiting verified evidence" }
            : { stage: "close", state: "later", summary: "Not closed" },
  ];
  // With no stage marked current, the first stage after the last finished one becomes the current one.
  if (c && !steps.some((s) => s.state === "current")) {
    const lastDone = steps.map((s) => s.state).lastIndexOf("complete");
    const next = steps[lastDone + 1];
    if (next && next.state === "later" && lastDone >= 1) next.state = "current";
  }
  return steps;
}

// ---------------------------------------------------------------- stepper
const GLYPH: Record<Stage, DuoName> = { read: "read", decide: "decide", plan: "plan", act: "engage", close: "complete" };

/** Duotone stage glyph on a glass tile. Finished stages carry a check badge, restricted ones a warning tone, later ones fade. */
function StepIcon({ state, stage }: { state: StepState; stage: Stage }) {
  return (
    <span aria-hidden="true" className={`relative ${state === "later" || state === "none" ? "opacity-55" : ""}`}>
      <GlassTile name={GLYPH[stage]} size="md" tone={state === "complete" ? "ok" : state === "unavailable" ? "warn" : "brand"} />
      {state === "complete" && (
        <span className="absolute -right-1 -bottom-1 grid size-5 place-items-center rounded-full bg-ok text-white ring-2 ring-surface">
          <Icon name="check" className="size-3" />
        </span>
      )}
      {state === "current" && <span className="absolute -top-1 -right-1 size-3 rounded-full bg-brand ring-2 ring-surface" />}
    </span>
  );
}
const STATE_WORD: Record<StepState, string> = { complete: "complete", current: "in progress", none: "no records", ended: "ended without completion", later: "not reached", unavailable: "restricted" };

function Stepper({ steps, active, onPick }: { steps: Step[]; active: Stage; onPick: (s: Stage) => void }) {
  return (
    <nav aria-label="Resident journey stages" className="card px-2 py-3 sm:px-4">
      <ol className="flex items-start">
        {steps.map((s, i) => (
          <li key={s.stage} className="relative flex min-w-0 flex-1 flex-col items-center">
            {i > 0 && <span aria-hidden="true" className={`absolute top-[2.15rem] right-1/2 h-0.5 w-full ${steps[i - 1].state === "complete" ? "bg-brand" : "bg-line"}`} />}
            <button
              type="button"
              onClick={() => onPick(s.stage)}
              aria-current={s.stage === active ? "step" : undefined}
              data-stage={s.stage}
              data-state={s.state}
              className={`relative z-10 flex w-full min-w-0 flex-col items-center gap-1.5 rounded-control px-1 py-2 text-center transition-colors ${s.stage === active ? "bg-brand-soft" : "hover:bg-canvas"}`}
            >
              <StepIcon state={s.state} stage={s.stage} />
              <span className="font-semibold">
                {STAGE_LABEL[s.stage]}
                <span className="sr-only">, {STATE_WORD[s.state]}</span>
              </span>
              <span className="hidden text-xs leading-snug text-ink-muted md:block md:max-w-44">{s.summary}</span>
            </button>
          </li>
        ))}
      </ol>
    </nav>
  );
}

// ---------------------------------------------------------------- panels
function Panel({ stage, step, children }: { stage: Stage; step: Step; children: ReactNode }) {
  return (
    <Card as="section" aria-labelledby={`h-${stage}`} className="mt-4">
      <CardHeader id={`h-${stage}`} title={`${STAGE_LABEL[stage]}: ${STAGE_BLURB[stage]}`} subtitle={step.summary} />
      <div className="px-5 pb-5">{children}</div>
    </Card>
  );
}

const Restricted = ({ what }: { what: string }) => (
  <p role="note" className="rounded-control border border-warn-line bg-warn-bg px-4 py-3 text-warn">
    {noRole}: {what}. The rest of the journey is unaffected.
  </p>
);
const Empty = ({ title, children }: { title: string; children?: ReactNode }) => (
  <div className="rounded-control border border-dashed border-line-strong px-4 py-5">
    <p className="font-semibold">{title}</p>
    {children && <p className="mt-1 max-w-[65ch] text-ink-muted">{children}</p>}
  </div>
);

function ReadPanel({ j }: { j: JourneyPayload }) {
  const [all, setAll] = useState(false);
  if (!j.evidence) return <Restricted what="evidence extracted from notes" />;
  const rows = j.evidence.facts;
  const shown = all ? rows : rows.slice(0, 6);
  if (rows.length === 0)
    return (
      <Empty title="No notes have been read for this resident">
        {j.resident.counts.documents > 0 ? `${j.resident.counts.documents} notes are registered but AI extraction has not processed them yet. ` : "No notes are registered. "}
        The decision stage can still use structured records.
      </Empty>
    );
  return (
    <>
      <ul className="space-y-2.5">
        {shown.map((f) => (
          <li key={f.id} className="rounded-control border border-line px-4 py-3">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="font-semibold">{humanize(f.fact_type)}</span>
              <StatusBadge status={f.assertion_status} />
              <Tag>{f.subject_type === "family_member" ? `Relative: ${humanize(f.relationship_type)}` : humanize(f.subject_type)}</Tag>
              <Tag>{humanize(f.temporal_status)}</Tag>
              <StatusBadge status={f.validation_status} />
              {f.sources[0] && <StatusBadge status={f.sources[0].verification_status} />}
            </div>
            {f.sources[0]?.quote && (
              <blockquote dir="auto" className="mt-2 rounded-control bg-canvas px-3 py-2 text-ink-soft">
                {f.sources[0].quote}
              </blockquote>
            )}
            {f.sources[0]?.document_id ? <ShowInNote factId={f.id} /> : <p className="mt-1 text-xs text-warn">No source document is stored for this fact.</p>}
          </li>
        ))}
      </ul>
      {rows.length > 6 && (
        <button type="button" className={`${btnSecondary} mt-3`} onClick={() => setAll((a) => !a)}>
          {all ? "Show fewer facts" : `Show all ${rows.length} facts`}
        </button>
      )}
      {j.evidence.truncated && <p className="mt-2 text-ink-muted">Only the first {rows.length} facts are loaded.</p>}
    </>
  );
}

function DecidePanel({ j, c }: { j: JourneyPayload; c: JourneyCase | undefined }) {
  const [pick, setPick] = useState<string | null>(null);
  if (!j.evaluations) return <Restricted what="guideline evaluations" />;
  const rows = j.evaluations.items.filter((e) => e.is_latest);
  if (rows.length === 0) return <Empty title="No guideline evaluation yet">Evaluations appear once the guideline engine has run for this resident.</Empty>;
  const defaultId = c?.evaluation_id ?? rows.find((e) => e.outcome_class === "eligible")?.id ?? rows[0].id;
  const explain = j.evaluations.items.find((e) => e.id === (pick ?? defaultId)) ?? rows[0];
  return (
    <>
      <ul aria-label="Latest guideline evaluations" className="mb-5 divide-y divide-line overflow-hidden rounded-control border border-line">
        {rows.map((e) => (
          <li key={e.id} className="flex flex-wrap items-center gap-2 px-4 py-2.5">
            <span className="min-w-44 font-semibold">{humanize(e.guideline?.care_activity)}</span>
            <StatusBadge status={e.outcome} />
            <span className="text-xs text-ink-muted">
              {e.guideline?.key} v{e.guideline?.version}
            </span>
            <button type="button" className={`${btnSecondary} ml-auto`} aria-pressed={e.id === explain.id} onClick={() => setPick(e.id)}>
              {e.id === explain.id ? "Explained below" : "Why?"}
            </button>
          </li>
        ))}
      </ul>
      <WhyDueBody key={explain.id} ev={fromJourneyEvaluation(explain)} />
    </>
  );
}

function PlanPanel({ c }: { c: JourneyCase | undefined }) {
  if (!c) return <Empty title="No care gap case yet">A case is opened when a rule finds the resident due. Nothing can be planned before that.</Empty>;
  if (c.allocations === null) return <Restricted what="planned allocations" />;
  const a = c.allocations[0];
  return (
    <div className="space-y-3">
      <p className="flex flex-wrap items-center gap-2">
        Case status <StatusBadge status={c.status} />
        <span className="text-ink-muted">{c.planning_run_id ? <>planning run <span className="font-mono text-xs">{c.planning_run_id.slice(0, 8)}</span></> : "not in a planning run yet"}</span>
      </p>
      {a ? (
        <dl aria-label="Planned allocation" className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 rounded-control border border-line bg-canvas px-4 py-3">
          <dt className="text-ink-muted">Clinic</dt>
          <dd>{a.clinic?.name ?? humanize(a.clinic?.key)} {a.clinic?.district_code ? <span className="text-ink-muted">({a.clinic.district_code})</span> : null}</dd>
          <dt className="text-ink-muted">Service</dt>
          <dd>{humanize(a.service_key)}</dd>
          <dt className="text-ink-muted">Proposed slot</dt>
          <dd>
            {fmtDateTime(a.proposed_start_time)} to {fmtDateTime(a.proposed_end_time).slice(11)}
          </dd>
          <dt className="text-ink-muted">Allocation</dt>
          <dd>
            <StatusBadge status={a.allocation_status} /> {a.confirmed_at ? <span className="text-ink-muted">confirmed {fmtDateTime(a.confirmed_at)}</span> : null}
          </dd>
          <dt className="text-ink-muted">Why this slot</dt>
          <dd>{a.explanation ?? "No explanation stored."}</dd>
        </dl>
      ) : (
        <Empty title={c.status === "awaiting_capacity" ? "Waiting for capacity" : "No slot proposed yet"}>
          {c.status === "awaiting_capacity" ? "The planner found no open slot that satisfies the constraints for this case." : "The capacity planner has not proposed a slot for this case."}
        </Empty>
      )}
      <Link href="/planning" className="inline-flex items-center gap-1 text-brand-ink underline underline-offset-2">
        Open capacity planning <Icon name="arrow-right" className="size-4" />
      </Link>
    </div>
  );
}

function ActPanel({ c }: { c: JourneyCase | undefined }) {
  const can = useCan();
  if (!c) return <Empty title="No outreach yet">Outreach is planned only for cases that have a slot.</Empty>;
  if (c.communications === null) return <Restricted what="outreach messages" />;
  const msgs = c.communications;
  const appts = c.appointments ?? [];
  return (
    <div className="space-y-5">
      {msgs.length === 0 ? (
        <Empty title="No outreach recorded">Messages appear once the case is planned. Every message is simulated and never reaches a real person.</Empty>
      ) : (
        <ul aria-label="Simulated messages" className="space-y-2.5">
          {msgs.map((m) => (
            <li key={m.id} className="rounded-control border border-dashed border-specimen bg-surface px-4 py-3">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-semibold">{humanize(m.channel)}</span>
                <StatusBadge status={m.delivery_state} />
                <Tag>{LANGUAGES[m.language] ?? m.language}</Tag>
                <DataLabel kind="simulated">Simulated message</DataLabel>
                <span className="ml-auto text-xs text-ink-muted">{fmtDateTime(m.sent_at ?? m.created_at)}</span>
              </div>
              <p lang={m.language} dir={m.direction === "rtl" ? "rtl" : dirFor(m.language)} className="mt-2 whitespace-pre-wrap">
                {m.body}
              </p>
              {m.responses.map((r) => (
                <p key={r.id} className="mt-2 rounded-control border border-line bg-canvas px-3 py-1.5">
                  Resident response (simulated): <span className="font-semibold">{humanize(r.response_type)}</span>
                  {r.response_value ? `, "${r.response_value}"` : ""}
                </p>
              ))}
            </li>
          ))}
        </ul>
      )}
      {appts.length > 0 && (
        <ul aria-label="Simulated appointments" className="space-y-1.5">
          {appts.map((a) => (
            <li key={a.id} className="flex flex-wrap items-center gap-2 rounded-control border border-line px-4 py-2.5">
              <Icon name="calendar-check" className="size-4 text-brand-ink" />
              <span className="font-semibold">{a.clinic?.name ?? humanize(a.clinic?.key)}</span>
              <span>{fmtDateTime(a.start_time)}</span>
              <StatusBadge status={a.appointment_status} />
              <DataLabel kind="simulated">Simulated booking</DataLabel>
            </li>
          ))}
        </ul>
      )}
      <OutreachPreview caseId={c.id} messages={msgs} />
      {can("engagement.view") && <CaseConversation caseId={c.id} />}
    </div>
  );
}

function ClosePanel({ c }: { c: JourneyCase | undefined }) {
  if (!c) return <Empty title="Nothing to close">Closure applies to a care gap case.</Empty>;
  const proofs = c.completion_evidence;
  const history: TimelineItem[] = c.history.map((h, i) => ({
    id: `${i}-${h.occurred_at}`,
    at: h.occurred_at,
    tone: h.to_status === "completed" ? "ok" : "neutral",
    title: (
      <span className="inline-flex flex-wrap items-center gap-1.5">
        {h.from_status ? <StatusBadge status={h.from_status} /> : <span className="text-ink-muted">Created as</span>}
        {h.from_status && <span aria-hidden="true">to</span>}
        <StatusBadge status={h.to_status} />
      </span>
    ),
    detail: [h.reason, h.actor_role ? `by ${humanize(h.actor_role)}` : "by the system"].filter(Boolean).join(", "),
  }));
  const completedNoProof = c.status === "completed" && proofs !== null && !proofs.some((p) => p.verification_status === "verified");
  return (
    <div className="grid gap-6 lg:grid-cols-2 *:min-w-0">
      <div>
        <h3 className="mb-3 font-semibold">Status history</h3>
        {history.length ? <Timeline label="Case status history" items={history} /> : <p className="text-ink-muted">No status changes recorded.</p>}
      </div>
      <div>
        <h3 className="mb-3 font-semibold">Completion evidence</h3>
        {proofs === null ? (
          <Restricted what="completion evidence" />
        ) : proofs.length === 0 ? (
          <Empty title="No completion evidence yet">A case can only be closed as completed once evidence of the screening has been verified.</Empty>
        ) : (
          <ul className="space-y-2">
            {proofs.map((p) => (
              <li key={p.id} className="rounded-control border border-line px-4 py-2.5">
                <p className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold">{humanize(p.activity)}</span>
                  <StatusBadge status={p.verification_status} />
                </p>
                <p className="text-xs text-ink-muted">
                  {humanize(p.source_type)}, reference <span className="font-mono">{p.evidence_reference}</span>
                  {p.verified_at ? `, verified ${fmtDateTime(p.verified_at)}` : ""}
                </p>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-4">
          Closure:{" "}
          {c.closed_at ? (
            <>
              <StatusBadge status={c.status} /> on {fmtDateTime(c.closed_at)}
              {c.closure_reason ? `: ${c.closure_reason}` : ""}
            </>
          ) : (
            <span className="text-ink-muted">still open ({humanize(c.status)})</span>
          )}
        </p>
        {completedNoProof && (
          <p role="alert" className="mt-2 text-danger">
            This case is marked completed but no verified completion evidence is on file. That should not be possible; report it.
          </p>
        )}
        <p className="mt-3">
          <Link href={`/care-gaps/${c.id}`} className="inline-flex items-center gap-1 text-brand-ink underline underline-offset-2">
            Open the full case <Icon name="arrow-right" className="size-4" />
          </Link>
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- view
export function JourneyView({ id }: { id: string }) {
  const res = useApi(`journey:${id}`, (s) => api<JourneyPayload>(`/residents/${id}/journey`, { signal: s }));
  const [pickedCase, setPickedCase] = useState<string | null>(null);
  const [pickedStage, setPickedStage] = useState<Stage | null>(null);

  const back = (
    <p className="mb-3">
      <Link href={`/residents/${id}`} className="inline-flex items-center gap-1 text-brand-ink underline underline-offset-2">
        Resident record
      </Link>
    </p>
  );
  if (res.error) {
    return (
      <>
        {back}
        <PageHeader title="Resident journey" />
        <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load this resident's journey" />
      </>
    );
  }
  const j = res.data?.data;
  if (!j)
    return (
      <p role="status" className="text-ink-muted">
        Loading the journey…
      </p>
    );

  const r = j.resident;
  const list = j.cases?.items ?? [];
  const c = list.find((x) => x.id === pickedCase) ?? list.find((x) => !TERMINAL.includes(x.status)) ?? list[0];
  const steps = deriveSteps(j, c);
  const current = steps.find((s) => s.state === "current")?.stage ?? [...steps].reverse().find((s) => s.state === "complete" || s.state === "ended")?.stage ?? "read";
  const active = pickedStage ?? current;
  const step = steps.find((s) => s.stage === active)!;

  return (
    <>
      {back}
      <PageHeader
        title={`Journey of resident ${r.id.slice(0, 8)}`}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <span>{[r.age != null ? `${r.age} years` : null, humanize(r.administrative_sex), r.district_code, r.preferred_language ? (LANGUAGES[r.preferred_language] ?? r.preferred_language) : null].filter((x) => x && x !== "None").join(", ")}</span>
            <span className="font-mono text-xs">{r.id}</span>
          </span>
        }
      />

      {j.cases === null ? (
        <p role="note" className="mb-4 rounded-control border border-warn-line bg-warn-bg px-4 py-2.5 text-warn">
          {noRole}: care gap cases. Plan, act and close are not shown.
        </p>
      ) : list.length > 1 ? (
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <label htmlFor="journey-case" className="font-medium">
            Care gap case
          </label>
          <select
            id="journey-case"
            value={c?.id ?? ""}
            onChange={(e) => {
              setPickedCase(e.target.value);
              setPickedStage(null);
            }}
            className={`${selectClass} max-w-md`}
          >
            {list.map((x) => (
              <option key={x.id} value={x.id}>
                {humanize(x.care_activity)}, {humanize(x.status)}
              </option>
            ))}
          </select>
        </div>
      ) : list.length === 1 ? (
        <p className="mb-4 flex flex-wrap items-center gap-2">
          Care gap case: <span className="font-semibold">{humanize(list[0].care_activity)}</span> <StatusBadge status={list[0].status} /> <PriorityPill priority={list[0].priority} />
        </p>
      ) : (
        <p className="mb-4 rounded-control border border-line bg-surface px-4 py-2.5 text-ink-muted">No care gap has been raised for this resident. Plan, act and close start when a case exists.</p>
      )}

      <Stepper steps={steps} active={active} onPick={setPickedStage} />

      {active === "read" && (
        <Panel stage="read" step={step}>
          <ReadPanel j={j} />
        </Panel>
      )}
      {active === "decide" && (
        <Panel stage="decide" step={step}>
          <DecidePanel j={j} c={c} />
        </Panel>
      )}
      {active === "plan" && (
        <Panel stage="plan" step={step}>
          <PlanPanel c={c} />
        </Panel>
      )}
      {active === "act" && (
        <Panel stage="act" step={step}>
          <ActPanel c={c} />
        </Panel>
      )}
      {active === "close" && (
        <Panel stage="close" step={step}>
          <ClosePanel c={c} />
        </Panel>
      )}

      <FamilyPedigree residentId={id} />
    </>
  );
}
