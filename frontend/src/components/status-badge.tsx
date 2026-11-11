import type { ReactNode } from "react";

type Tone = "ok" | "warn" | "danger" | "info" | "neutral";

const TONE: Record<Tone, string> = {
  ok: "text-ok bg-ok-bg border-ok",
  warn: "text-warn bg-warn-bg border-warn",
  danger: "text-danger bg-danger-bg border-danger",
  info: "text-info bg-info-bg border-info",
  neutral: "text-neutral bg-neutral-bg border-neutral",
};

// Shape + label + colour: status is never conveyed by colour alone.
const ICON: Record<string, ReactNode> = {
  circle: <circle cx="6" cy="6" r="4" fill="none" stroke="currentColor" strokeWidth="1.5" />,
  dotted: <circle cx="6" cy="6" r="4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeDasharray="2 2" />,
  play: <path d="M3.5 2.5v7l6-3.5z" fill="currentColor" />,
  half: (
    <>
      <circle cx="6" cy="6" r="4" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6 2a4 4 0 0 1 0 8z" fill="currentColor" />
    </>
  ),
  check: <path d="M2.5 6.5l2.5 2.5 4.5-5" fill="none" stroke="currentColor" strokeWidth="1.8" />,
  cross: <path d="M3 3l6 6M9 3l-6 6" fill="none" stroke="currentColor" strokeWidth="1.8" />,
  slash: (
    <>
      <circle cx="6" cy="6" r="4" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M3.2 8.8l5.6-5.6" stroke="currentColor" strokeWidth="1.5" />
    </>
  ),
  bang: <path d="M6 2v5M6 9.2v.1" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />,
};

const MAP: Record<string, { label: string; tone: Tone; icon: keyof typeof ICON }> = {
  // job states (contract §3 processing_jobs.status)
  pending: { label: "Pending", tone: "neutral", icon: "circle" },
  queued: { label: "Queued", tone: "neutral", icon: "dotted" },
  running: { label: "Running", tone: "info", icon: "play" },
  partially_completed: { label: "Partially completed", tone: "warn", icon: "half" },
  completed: { label: "Completed", tone: "ok", icon: "check" },
  failed: { label: "Failed", tone: "danger", icon: "cross" },
  cancelled: { label: "Cancelled", tone: "neutral", icon: "slash" },
  // integration health (05 §26.2)
  healthy: { label: "Healthy", tone: "ok", icon: "check" },
  degraded: { label: "Degraded", tone: "warn", icon: "half" },
  unavailable: { label: "Unavailable", tone: "danger", icon: "cross" },
  not_configured: { label: "Not configured", tone: "neutral", icon: "dotted" },
  authentication_failed: { label: "Authentication failed", tone: "danger", icon: "bang" },
  unknown: { label: "Unknown", tone: "neutral", icon: "circle" },
  // audit outcomes
  success: { label: "Success", tone: "ok", icon: "check" },
  failure: { label: "Failure", tone: "danger", icon: "cross" },
  denied: { label: "Denied", tone: "warn", icon: "bang" },
  // evidence validation and quote verification (07 §7.3)
  accepted: { label: "Accepted", tone: "ok", icon: "check" },
  rejected: { label: "Rejected", tone: "danger", icon: "cross" },
  requires_review: { label: "Requires review", tone: "warn", icon: "bang" },
  schema_valid: { label: "Schema valid", tone: "info", icon: "dotted" },
  source_verified: { label: "Source verified", tone: "info", icon: "check" },
  verified_exact: { label: "Quote verified (exact)", tone: "ok", icon: "check" },
  verified_normalized: { label: "Quote verified (normalised)", tone: "ok", icon: "check" },
  span_mismatch: { label: "Span mismatch", tone: "danger", icon: "cross" },
  quote_not_found: { label: "Quote not found", tone: "danger", icon: "cross" },
  source_missing: { label: "Source missing", tone: "danger", icon: "cross" },
  verification_error: { label: "Verification error", tone: "danger", icon: "bang" },
  // assertion status of an extracted fact (07): a negated statement is the opposite of a positive one
  positive: { label: "Stated", tone: "info", icon: "check" },
  negative: { label: "Negated", tone: "neutral", icon: "cross" },
  uncertain: { label: "Uncertain", tone: "warn", icon: "bang" },
  conditional: { label: "Conditional", tone: "warn", icon: "half" },
  // guideline versions, care-gap cases and allocations
  draft: { label: "Draft", tone: "neutral", icon: "dotted" },
  review: { label: "In review", tone: "warn", icon: "half" },
  in_review: { label: "In review", tone: "warn", icon: "half" },
  approved: { label: "Approved", tone: "ok", icon: "check" },
  retired: { label: "Retired", tone: "neutral", icon: "slash" },
  pending_review: { label: "Pending review", tone: "warn", icon: "bang" },
  approved_for_planning: { label: "Approved for planning", tone: "info", icon: "check" },
  awaiting_capacity: { label: "Awaiting capacity", tone: "warn", icon: "half" },
  open: { label: "Open", tone: "ok", icon: "check" },
  closed: { label: "Closed", tone: "neutral", icon: "slash" },
  identified: { label: "Identified", tone: "neutral", icon: "circle" },
  planned: { label: "Planned", tone: "info", icon: "half" },
  outreach_pending: { label: "Outreach pending", tone: "info", icon: "dotted" },
  outreach_completed: { label: "Outreach completed", tone: "info", icon: "check" },
  appointment_scheduled: { label: "Appointment scheduled", tone: "info", icon: "check" },
  awaiting_completion_evidence: { label: "Awaiting completion evidence", tone: "warn", icon: "half" },
  escalated: { label: "Escalated", tone: "danger", icon: "bang" },
  closed_not_completed: { label: "Closed, not completed", tone: "neutral", icon: "slash" },
  superseded: { label: "Superseded", tone: "neutral", icon: "slash" },
  eligible: { label: "Eligible", tone: "info", icon: "check" },
  requires_clinical_review: { label: "Requires clinical review", tone: "warn", icon: "bang" },
  insufficient_evidence: { label: "Insufficient evidence", tone: "warn", icon: "dotted" },
  proposed: { label: "Proposed", tone: "info", icon: "dotted" },
  confirmed: { label: "Confirmed", tone: "ok", icon: "check" },
};

export function StatusBadge({ status }: { status: string }) {
  const key = status.toLowerCase();
  const m = MAP[key] ?? {
    label: status.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()),
    tone: "neutral" as Tone,
    icon: "circle",
  };
  return (
    <span
      data-status={key}
      className={`inline-flex items-center gap-1 rounded-sm border px-1.5 text-xs font-medium leading-5 whitespace-nowrap ${TONE[m.tone]}`}
    >
      <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
        {ICON[m.icon]}
      </svg>
      {m.label}
    </span>
  );
}
