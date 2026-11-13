/**
 * Owner rule: no hyphen-as-dash, en dash or em dash anywhere in UI text. Server prose (rule explanations, definitions,
 * notices) often uses spaced dashes as punctuation, so render all backend text through this helper.
 *
 *   displayText("Capacity is full — add sessions") -> "Capacity is full, add sessions"
 *   displayText("Eligible at 50-74")                      -> "Eligible at 50 to 74"
 *
 * Only spaced dashes and en/em dashes are rewritten. Hyphens inside identifiers and codes ("syn-20261105-s42",
 * "HTN-01") are data and stay exactly as they are. Do not use it on identifiers.
 */
export function displayText(text: string | null | undefined): string {
  if (!text) return "";
  return text
    // number ranges first: "50-74" -> "50 to 74" (not dates such as 2026-11-05, which keep three or more groups)
    .replace(/(?<![\d-])(\d{1,3})\s*[-–—]\s*(\d{1,3})(?![\d-])/g, "$1 to $2")
    .replace(/\s*[–—]\s*/g, ", ")
    .replace(/\s+-\s+/g, ", ")
    .replace(/,\s*,/g, ",")
    .replace(/\s+,/g, ",")
    .trim();
}
