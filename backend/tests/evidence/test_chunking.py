"""Offset-preserving chunking (retrieval only)."""

from __future__ import annotations

import hashlib

import pytest
from evidence_samples import SAMPLES

from anticipa.evidence.chunking import chunk_text


def assert_invariants(text: str, chunks) -> None:
    last_end = 0
    for i, c in enumerate(chunks):
        assert c.index == i and 0 <= c.start < c.end <= len(text)
        assert text[c.start : c.end] == c.text == c.text.strip()
        assert c.content_hash == hashlib.sha256(c.text.encode("utf-8")).hexdigest()
        assert c.start >= last_end  # ordered, non-overlapping
        last_end = c.end


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: s.key)
def test_short_documents_are_one_chunk_with_exact_offsets(sample):
    chunks = chunk_text(sample.text)
    assert_invariants(sample.text, chunks)
    assert len(chunks) == 1 and chunks[0].text == sample.text.strip()


def test_long_document_splits_on_sentence_ends_in_every_script():
    for sentence in (
        "Her mother was diagnosed with breast cancer at age 45. ",
        "उनकी माँ को 45 वर्ष की आयु में स्तन कैंसर का निदान हुआ था। ",
        "تم تشخيص الأم بسرطان الثدي في سن 45. ",
    ):
        text = (sentence * 40).strip()
        chunks = chunk_text(text, target_chars=300, max_chars=600)
        assert_invariants(text, chunks)
        assert len(chunks) > 3
        assert all(c.text.endswith((".", "।")) for c in chunks)  # never mid-sentence
        assert "".join(c.text.replace(" ", "") for c in chunks) == text.replace(" ", "")  # nothing lost


def test_blank_lines_are_boundaries_and_arabic_question_mark_splits():
    text = "First paragraph.\n\nSecond paragraph?\n   \nثالثا؟ رابعا."
    chunks = chunk_text(text, target_chars=10, max_chars=60)
    assert_invariants(text, chunks)
    assert [c.text for c in chunks] == ["First paragraph.", "Second paragraph?", "ثالثا؟", "رابعا."]


def test_one_overlong_sentence_is_hard_split_at_whitespace():
    text = " ".join(["word"] * 400)  # no sentence end at all
    chunks = chunk_text(text, target_chars=300, max_chars=500)
    assert_invariants(text, chunks)
    assert len(chunks) >= 4 and all(len(c.text) <= 500 for c in chunks)
    assert " ".join(c.text for c in chunks) == text


@pytest.mark.parametrize("text", ["", "   \n\t  ", "\n\n"])
def test_blank_text_has_no_chunks(text):
    assert chunk_text(text) == []
