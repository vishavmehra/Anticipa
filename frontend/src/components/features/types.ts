// Types for the judge-facing feature components. Aliases of the generated API types where the backend is typed
// (src/lib/api-types.ts, regenerate with `npm run gen:types`); hand-written only where a payload is an untyped dict,
// each marked with the backend file its keys come from. Purpose: show judges the completed screens.

import type { components } from "@/lib/api-types";

type S = components["schemas"];

export type EvaluationRecord = S["EvaluationOut"];
export type FactRecord = S["FactOut"];
export type CaseRecord = S["CaseOut"];
export type CaseDetailRecord = S["CaseDetailOut"];
export type CommunicationRecord = S["CommunicationOut"];
export type AppointmentRecord = S["AppointmentOut"];
export type CompletionEvidenceRecord = S["EvidenceOut"];
export type AllocationRecord = S["AllocationOut"];
export type UnallocatedRecord = S["UnallocatedOut"];
export type ResidentRecord = S["ResidentDetail"];

/** The five pipeline stages, in order (READ -> DECIDE -> PLAN -> ACT -> CLOSE). */
export const STAGES = ["read", "decide", "plan", "act", "close"] as const;
export type Stage = (typeof STAGES)[number];

export const STAGE_LABEL: Record<Stage, string> = { read: "Read", decide: "Decide", plan: "Plan", act: "Act", close: "Close" };
export const STAGE_BLURB: Record<Stage, string> = {
  read: "AI extracts facts, each with an exact quote",
  decide: "Versioned rules decide who is due",
  plan: "Capacity planner proposes a slot",
  act: "Simulated multilingual outreach",
  close: "Closed only with verified completion evidence",
};

/** Which stage a case status belongs to (backend/anticipa/caregaps/lifecycle.py ALLOWED). */
export const STATUS_STAGE: Record<string, Stage> = {
  identified: "decide",
  pending_review: "decide",
  approved_for_planning: "decide",
  escalated: "decide",
  awaiting_capacity: "plan",
  planned: "plan",
  outreach_pending: "act",
  outreach_completed: "act",
  appointment_scheduled: "act",
  awaiting_completion_evidence: "close",
  completed: "close",
  closed_not_completed: "close",
  cancelled: "close",
};
