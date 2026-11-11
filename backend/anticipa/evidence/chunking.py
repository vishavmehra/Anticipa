"""Offset-preserving chunking for retrieval embeddings (07 §5.5). Used by `evidence.embed_documents` for
semantic search only: extraction sees the whole document; chunks carry no clinical meaning on their own.

Chunk boundaries fall on sentence ends (Latin `.!?`, Arabic `؟`, Devanagari danda `।`) and blank lines, so a
negation, a relationship word and the condition they qualify stay in one sentence and (if it fits) one chunk.
Invariant: `text[chunk.start:chunk.end] == chunk.text` (Python code-point offsets, half-open), and
`content_hash` is the sha256 of that exact slice.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

TARGET_CHARS = 900  # pack sentences until a chunk reaches this size (~250-350 tokens in en/hi/ar)
MAX_CHARS = 1400  # a single over-long sentence is hard-split at whitespace below this size

_BOUNDARY = re.compile(r"(?<=[.!?؟।])\s+|\n\s*\n")


@dataclass(frozen=True)
class Chunk:
    index: int
    start: int
    end: int
    text: str
    content_hash: str


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Non-empty (start, end) spans of the sentences, whitespace excluded, in order, covering all text."""
    spans, pos = [], 0
    for m in [*_BOUNDARY.finditer(text), None]:
        end = m.start() if m else len(text)
        while pos < end and text[pos].isspace():
            pos += 1
        stop = end
        while stop > pos and text[stop - 1].isspace():
            stop -= 1
        if stop > pos:
            spans.append((pos, stop))
        if m:
            pos = m.end()
    return spans


def _split_long(text: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    out = []
    while end - start > max_chars:
        cut = text.rfind(" ", start + max_chars // 2, start + max_chars)
        cut = start + max_chars if cut == -1 else cut
        out.append((start, cut))
        start = cut
        while start < end and text[start].isspace():
            start += 1
    if end > start:
        out.append((start, end))
    return out


def chunk_text(text: str, *, target_chars: int = TARGET_CHARS, max_chars: int = MAX_CHARS) -> list[Chunk]:
    pieces: list[tuple[int, int]] = []
    for s, e in _sentence_spans(text):
        pieces.extend(_split_long(text, s, e, max_chars) if e - s > max_chars else [(s, e)])

    groups: list[tuple[int, int]] = []
    for s, e in pieces:
        if groups and e - groups[-1][0] <= target_chars:
            groups[-1] = (groups[-1][0], e)
        else:
            groups.append((s, e))
    return [
        Chunk(i, s, e, text[s:e], hashlib.sha256(text[s:e].encode("utf-8")).hexdigest())
        for i, (s, e) in enumerate(groups)
    ]
