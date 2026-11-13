// Display helpers for Ask Anticipa. House style: no dashes in UI text, so every string that comes from the server or from
// a tool definition is passed through plain() before it is shown.

/** Removes em and en dashes and spaced hyphens (a comma instead) and hyphens inside words (a space), as the backend does; a second line of defence. */
export const plain = (s: string): string =>
  s
    .replace(/\s*[–—]\s*/g, ", ")
    .replace(/\s+-\s+/g, ", ")
    .replace(/(?<=[A-Za-z])-(?=[A-Za-z])/g, " ")
    .trim();

const TOOL_LABELS: Record<string, string> = {
  get_overview_kpis: "Headline measures",
  get_funnel: "Care gap funnel",
  get_geography: "Districts and states",
  get_programme_stats: "Programme statistics",
  get_care_gap_breakdown: "Care gap breakdown",
  get_closure_trend: "Closure trend",
  get_capacity_overview: "Clinic capacity",
  get_engagement_overview: "Outreach and responses",
  get_evaluation_summary: "Evaluation results",
};
export const toolLabel = (tool: string): string => TOOL_LABELS[tool] ?? tool.replace(/^get_/, "").replace(/_/g, " ");

const SOURCE_LABELS: Record<string, string> = {
  simulated: "Simulated data",
  synthetic: "Generated data",
  live: "Live system state",
  evaluation: "Evaluation results",
};
export const sourceLabel = (s: string): string => SOURCE_LABELS[s] ?? s;

/** One value for a chart or table cell, by the unit the server declared for its field. */
export function formatValue(v: string | number | null | undefined, unit?: string | null): string {
  if (v === null || v === undefined || v === "") return "No data";
  if (typeof v === "string") return plain(v.replace(/_/g, " "));
  if (unit === "ratio") return `${(v * 100).toLocaleString("en-US", { maximumFractionDigits: Math.abs(v) < 0.1 ? 2 : 1 })}%`; // two decimals below 10%, so 0.0055 reads 0.55%, never a rounded 0.5% or 0.6%
  return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

/** Arguments of a tool call as readable "name: value" parts ("group by: district"). */
export function argSummary(args: Record<string, unknown>): string {
  const parts = Object.entries(args).map(([k, v]) => `${k.replace(/_/g, " ")}: ${String(v)}`);
  return parts.length ? parts.join(", ") : "No filters";
}

/** "claude-haiku-5-5" becomes "Claude Haiku 5.5" (no hyphens in UI text). */
export function modelLabel(id: string): string {
  const m = id.match(/^claude-([a-z]+)-(\d+)-(\d+)$/);
  return m ? `Claude ${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}` : plain(id.replace(/-/g, " "));
}
