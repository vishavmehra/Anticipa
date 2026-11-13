// Shared bits for the judge-facing feature components (journey, explainer, note viewer, funnel, monitor, pedigree,
// outreach preview, evaluation). Callers: every file in this folder. Purpose: show judges the completed screens.
// Built on the design system in src/components/ui (Card, Pill, DataLabel, charts) and its semantic tokens.

import type { ReactNode } from "react";
import { displayText } from "@/lib/display";
import { humanize } from "@/lib/format";
import { DataLabel, Pill } from "@/components/ui";

/** Quiet metadata chip: for facts that are labels (subject, timing), not states (those use StatusBadge). */
export function Tag({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span title={title}>
      <Pill tone="neutral">{children}</Pill>
    </span>
  );
}

/** Where a figure comes from. Owner rule: metrics say Live (counted now) or Evaluation (measured against planted ground truth); "simulated" only marks outreach and booking events. */
export type Provenance = "live" | "evaluation" | "simulated";

/** Data-source label. Thin wrapper over the design system's DataLabel. */
export function SourceLabel({ kind, children }: { kind: Provenance; children?: ReactNode }) {
  return <DataLabel kind={kind === "evaluation" ? "measured" : kind}>{children}</DataLabel>;
}

/** Text direction for resident-language content: Arabic is right to left, Devanagari stays intact. */
export const dirFor = (lang: string | null | undefined): "rtl" | "ltr" | "auto" => (lang && /^(ar|he|fa|ur)\b/i.test(lang) ? "rtl" : "auto");

/** Whole seconds as "1 h 05 min" / "4 min 12 s" for ETAs. */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "Not available";
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${String(m % 60).padStart(2, "0")} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

export const fmtUsd = (v: number | null | undefined, digits = 2): string => (v == null ? "Not available" : `$${v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`);

/** Small heading used inside cards (the page owns the single h1). */
export function CardTitle({ id, children, aside }: { id?: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
      <h3 id={id} className="font-semibold">
        {children}
      </h3>
      {aside}
    </div>
  );
}

/**
 * Backend prose for display: owner rule is no hyphen or dash in UI text. displayText() handles spaced dashes; this also
 * joins common prefixes ("re-evaluation" becomes "reevaluation") and turns other hyphens between letters into spaces.
 * Hyphens next to digits (codes, dataset ids) are data and stay. Never use it on quotes from notes or on identifiers.
 */
export function plain(text: string | null | undefined): string {
  return displayText((text ?? "").replace(/(?<![\d-])(\d{1,3})\s*[-–—]\s*(\d{1,3})(?![\d-])/g, "$1 to $2"))
    .replace(/(?<![A-Za-z])(re|non|pre|co|anti)-(?=[a-z])/gi, "$1")
    .replace(/([A-Za-z])-(?=[A-Za-z])/g, "$1 ");
}

/** Share of a whole: whole percent, but one decimal below 1% so a small real share never reads as zero. */
export function fmtShare(f: number | null | undefined): string {
  if (f == null) return "Not available";
  const p = f * 100;
  return p > 0 && p < 1 ? `${p.toFixed(1)}%` : `${Math.round(p)}%`;
}

/** Rule and criterion ids as words: "age_50_74" reads "Age 50 to 74", "no_mammogram_in_24_months" reads "No mammogram in 24 months". */
export const idWords = (id: string | null | undefined): string => humanize((id ?? "").replace(/(\d+)_(\d+)/g, "$1 to $2"));
