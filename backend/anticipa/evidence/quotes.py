"""Deterministic quote verification (CLAUDE.md §10, docs/17 §6, 07 §7). No model, no fuzzy matching.

Statuses are exactly those of 07 §7.3:
  verified_exact       the quote is a substring of the original text
  verified_normalized  matched only after the documented normalisation below
  span_mismatch        quote is in the document but not where the caller said it was (`expected_span`)
  quote_not_found      the quote is blank or absent from the document
  source_missing       there is no document text to verify against
  verification_error   inputs were unusable (non-text)
Several hits are NOT a separate status: `location_ambiguous=True` and every span is recorded.
Only verified_exact / verified_normalized may be accepted downstream.

Offset convention: Python `str` code-point indices into the ORIGINAL document text, half-open [start, end).
Model-provided offsets are never trusted; spans are always recomputed here.

Normalisation (both sides, only if the exact search fails): Unicode NFC applied per base+combining-mark
cluster so offsets stay mappable; runs of whitespace (incl. NBSP) collapsed to one space with the ends
trimmed; invisible format characters removed (zero-width space/joiners, bidi marks, BOM, soft hyphen).
Case, punctuation, digits and word order are never folded, so a paraphrase or an edited sentence cannot
pass as a quotation. Limitation: NFC across separate starters (e.g. Hangul jamo) is not applied.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal

Status = Literal[
    "verified_exact",
    "verified_normalized",
    "span_mismatch",
    "quote_not_found",
    "source_missing",
    "verification_error",
]
Span = tuple[int, int]
MAX_SPANS = 20  # recorded hits are capped; location_ambiguous still says "several"

# ZWSP ZWNJ ZWJ LRM RLM, bidi embeddings/overrides, word joiner, BOM, soft hyphen
_INVISIBLE = frozenset([0x200B, 0x200C, 0x200D, 0x200E, 0x200F, *range(0x202A, 0x202F), 0x2060, 0xFEFF, 0xAD])


@dataclass(frozen=True)
class QuoteVerification:
    status: Status
    method: Literal["exact", "normalized", "none"] = "none"
    spans: tuple[Span, ...] = ()
    location_ambiguous: bool = False

    @property
    def verified(self) -> bool:
        return self.status in ("verified_exact", "verified_normalized")

    @property
    def span(self) -> Span | None:
        """The single span, or None when unverified or the quote occurs in several places."""
        return self.spans[0] if len(self.spans) == 1 and not self.location_ambiguous else None


def _find_all(hay: str, needle: str) -> list[int]:
    hits, i = [], hay.find(needle)
    while i != -1 and len(hits) <= MAX_SPANS:
        hits.append(i)
        i = hay.find(needle, i + 1)  # overlapping hits count: they make a short quote ambiguous
    return hits


def normalize_with_map(text: str) -> tuple[str, list[int], list[int]]:
    """Return (normalised_text, orig_start_per_char, orig_end_per_char) under the procedure above."""
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ord(ch) in _INVISIBLE:
            i += 1
        elif ch.isspace():
            j = i
            while j < n and (text[j].isspace() or ord(text[j]) in _INVISIBLE):
                j += 1
            if chars and chars[-1] != " ":
                chars.append(" ")
                starts.append(i)
                ends.append(j)
            i = j
        else:
            j = i + 1  # a base character plus its combining marks (matras, nukta, harakat, ...)
            while j < n and unicodedata.combining(text[j]):
                j += 1
            for c in unicodedata.normalize("NFC", text[i:j]):
                chars.append(c)
                starts.append(i)
                ends.append(j)
            i = j
    if chars and chars[-1] == " ":
        chars.pop(), starts.pop(), ends.pop()
    return "".join(chars), starts, ends


def _result(
    method: Literal["exact", "normalized"], spans: tuple[Span, ...], n_hits: int, expected_span: Span | None
) -> QuoteVerification:
    ambiguous = n_hits > 1
    if expected_span is not None and expected_span not in spans:
        return QuoteVerification("span_mismatch", method, spans, ambiguous)
    status: Status = "verified_exact" if method == "exact" else "verified_normalized"
    return QuoteVerification(status, method, spans, ambiguous)


def verify_quote(document_text: str, quote: str, *, expected_span: Span | None = None) -> QuoteVerification:
    """Verify `quote` against the original `document_text`. Never raises.

    `expected_span` is an optional claimed location (e.g. from an earlier run): if the quote is present
    but not at that span the result is `span_mismatch`, which is not acceptable as verified evidence.
    """
    if not isinstance(document_text, str) or not isinstance(quote, str):
        return QuoteVerification("verification_error")
    if not document_text.strip():
        return QuoteVerification("source_missing")
    if not quote.strip():
        return QuoteVerification("quote_not_found")

    hits = _find_all(document_text, quote)
    if hits:
        spans = tuple((h, h + len(quote)) for h in hits[:MAX_SPANS])
        return _result("exact", spans, len(hits), expected_span)

    norm_quote = normalize_with_map(quote)[0]
    if not norm_quote:
        return QuoteVerification("quote_not_found")
    norm_doc, starts, ends = normalize_with_map(document_text)
    hits = _find_all(norm_doc, norm_quote)
    if not hits:
        return QuoteVerification("quote_not_found")
    spans = tuple((starts[h], ends[h + len(norm_quote) - 1]) for h in hits[:MAX_SPANS])
    return _result("normalized", spans, len(hits), expected_span)
