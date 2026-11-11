const RTL = /^(ar|he|fa|ur)\b/i;

/**
 * Shows a source quote highlighted inside its surrounding document text (CLAUDE.md §10: every fact
 * needs a verifiable source quote). `start`/`end` are Unicode code-point offsets (Python str
 * indices) into `text`, end-exclusive; we slice by code points so astral characters stay aligned.
 * Highlight = background + underline (not colour alone) and no horizontal padding or weight change,
 * so Arabic letter joining survives the element boundary.
 */
export function EvidenceQuote({
  text,
  start,
  end,
  quote,
  lang,
  context = 90,
  shift = 0,
}: {
  text: string;
  start: number;
  end: number;
  /** The stored quote; when given, the offsets are checked against it. */
  quote?: string;
  /** BCP-47 language of the document: "en", "hi", "ar". */
  lang?: string;
  /** Characters of surrounding text on each side. */
  context?: number;
  /** When `text` is an excerpt, the excerpt's start offset in the full document; only changes the caption. */
  shift?: number;
}) {
  const cps = Array.from(text);
  const valid = Number.isInteger(start) && Number.isInteger(end) && start >= 0 && start < end && end <= cps.length;
  const dir = lang && RTL.test(lang) ? "rtl" : "auto";

  if (!valid) {
    return (
      <figure className="rounded-md border border-warn bg-warn-bg px-3 py-2 text-warn">
        <p className="font-medium">Source offsets are outside this document, so the quote cannot be located.</p>
        {quote && (
          <blockquote lang={lang} dir={dir} className="mt-1 text-ink">
            {quote}
          </blockquote>
        )}
      </figure>
    );
  }

  // Trim the context window to whole words so it never opens or closes mid-cluster (Devanagari, Arabic).
  const isSpace = (c: string) => /\s/.test(c);
  let from = Math.max(0, start - context);
  while (from > 0 && from < start && !isSpace(cps[from - 1])) from++;
  let to = Math.min(cps.length, end + context);
  while (to < cps.length && to > end && !isSpace(cps[to])) to--;
  const hit = cps.slice(start, end).join("");
  const mismatch = quote !== undefined && hit.normalize("NFC") !== quote.normalize("NFC");

  return (
    <figure className="rounded-md border border-line bg-surface">
      <blockquote lang={lang} dir={dir} className="px-3 py-2.5 leading-7 text-ink">
        {from > 0 && "… "}
        {cps.slice(from, start).join("")}
        <mark className="bg-mark text-ink shadow-[inset_0_-2px_0_var(--color-ink)]">{hit}</mark>
        {cps.slice(end, to).join("")}
        {to < cps.length && " …"}
      </blockquote>
      <figcaption className="tnum flex flex-wrap items-center gap-x-3 border-t border-line px-3 py-1.5 text-xs text-ink-muted">
        <span>
          Characters {start + shift}–{end + shift}
        </span>
        {quote !== undefined &&
          (mismatch ? (
            <span className="font-medium text-danger">Offsets do not match the stored quote: unverified</span>
          ) : (
            <span className="font-medium text-ok">Matches the stored quote</span>
          ))}
      </figcaption>
    </figure>
  );
}
