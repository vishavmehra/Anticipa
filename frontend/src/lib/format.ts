// Display formatting. Owner rules: dates read like people write them ("12 Nov 2026"), and an empty value says what it is
// ("None", "Not recorded", "Not available") instead of showing a dash.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const parts = (d: Date) => ({ day: d.getUTCDate(), mon: MONTHS[d.getUTCMonth()], year: d.getUTCFullYear() });

/** API timestamps are UTC ISO 8601 (contract §2). Shown as "12 Nov 2026, 18:42 UTC" so server and client agree. */
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "None";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Not recorded";
  const p = parts(d);
  return `${p.day} ${p.mon} ${p.year}, ${d.toISOString().slice(11, 16)} UTC`;
}

/** "12 Nov 2026". Calendar dates (YYYY-MM-DD, such as birth_date) are not shifted by timezone. */
export function fmtDate(d: string | null | undefined): string {
  if (!d) return "None";
  const m = d.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return "Not recorded";
  return `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}`;
}

/** "12 Nov" for chart axes and compact lists. */
export function fmtDayMonth(d: string | null | undefined): string {
  const m = d?.match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]}` : "";
}

export const fmtInt = (n: number | null | undefined): string => (n === null || n === undefined ? "Not available" : n.toLocaleString("en-US"));

/** Fractions from the API (0..1) as a whole percent; a missing measurement is never shown as zero. */
export const fmtPct = (f: number | null | undefined): string => (f == null ? "Not available" : f > 0 && f < 0.1 ? `${(f * 100).toFixed(1)}%` : `${Math.round(f * 100)}%`);

export function pct(done: number, total: number | null | undefined): number | null {
  if (!total || total <= 0) return null;
  return Math.min(100, Math.round((done / total) * 100));
}

/** "care_gap_case" -> "Care gap case". Machine names become plain words. */
export const humanize = (s: string | null | undefined): string => (s ? s.replace(/[_-]+/g, " ").replace(/^./, (c) => c.toUpperCase()) : "None");

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
  if (/[؀-ۿ]/.test(text)) return "ar";
  if (/[ऀ-ॿ]/.test(text)) return "hi";
  return undefined;
}

/** "30-39" -> "30 to 39", "80+" -> "80 and over". Age bands are data ranges, shown as plain words. */
export function ageBandLabel(key: string | null | undefined): string {
  if (!key) return "None";
  const m = key.match(/^(\d+)-(\d+)$/);
  if (m) return `${m[1]} to ${m[2]}`;
  const p = key.match(/^(\d+)\+$/);
  return p ? `${p[1]} and over` : humanize(key);
}
