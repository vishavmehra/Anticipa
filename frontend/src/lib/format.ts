/** API timestamps are UTC ISO 8601 (contract §2). Render them as UTC so server/client output is identical. */
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().slice(0, 10)} ${d.toISOString().slice(11, 19)} UTC`;
}

/** Calendar dates (birth_date) are rendered as given, not shifted by timezone. */
export const fmtDate = (d: string | null | undefined): string => (d ? d.slice(0, 10) : "—");

export const fmtInt = (n: number | null | undefined): string =>
  n === null || n === undefined ? "—" : n.toLocaleString("en-US");

export function pct(done: number, total: number | null | undefined): number | null {
  if (!total || total <= 0) return null;
  return Math.min(100, Math.round((done / total) * 100));
}

export const humanize = (s: string | null | undefined): string =>
  s ? s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : "—";

/** Whole years between a birth date (YYYY-MM-DD) and today, computed in UTC. null when unknown. */
export function ageYears(birth: string | null | undefined, today: Date = new Date()): number | null {
  const m = birth?.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return null;
  let age = today.getUTCFullYear() - +m[1];
  if (today.getUTCMonth() + 1 < +m[2] || (today.getUTCMonth() + 1 === +m[2] && today.getUTCDate() < +m[3])) age--;
  return age >= 0 ? age : null;
}

export const LANGUAGES: Record<string, string> = { en: "English", hi: "Hindi", ar: "Arabic" };

/** Script-based fallback when the API does not say which language a document is in. */
export function guessLang(text: string): string | undefined {
  if (/[\u0600-\u06FF]/.test(text)) return "ar";
  if (/[\u0900-\u097F]/.test(text)) return "hi";
  return undefined;
}

/** Fractions from the API (0..1) as a whole percent; null stays "—" so a missing measurement is never a zero. */
export const fmtPct = (f: number | null | undefined): string => (f == null ? "—" : `${Math.round(f * 100)}%`);
