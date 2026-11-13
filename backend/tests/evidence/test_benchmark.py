# ruff: noqa: E501  (multilingual query strings)
"""The retrieval benchmark: metric maths, the zero-spend estimate, the refusal rules, and both rankers offline."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from anticipa.ai.settings import AiSettings
from anticipa.ai.testing import FakeAdapter, make_test_router
from anticipa.ai.types import Usage
from anticipa.evidence import benchmark as bm
from anticipa.evidence import service


def test_metric_maths():
    assert bm.recall_at_k(["a", "b", "c"], {"a", "c", "z"}, 2) == pytest.approx(1 / 3)
    assert bm.recall_at_k(["a"], set(), 3) == 0.0
    assert (
        bm.reciprocal_rank(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)
        and bm.reciprocal_rank(["x"], {"a"}) == 0.0
    )
    assert (
        bm.cosine([1, 0], [1, 0]) == 1.0
        and bm.cosine([1, 0], [0, 1]) == 0.0
        and bm.cosine([0, 0], [1, 1]) == 0.0
    )
    assert bm.doc_ranking([("b", 0.5), ("a", 0.5), ("b", 0.9), ("c", 0.1)]) == [
        "b",
        "a",
        "c",
    ]  # best chunk, ties by key
    rel = {"c1": {"d1"}}
    s = bm.summarise({("c1", "en"): ["d1"], ("c1", "hi"): ["x", "d1"]}, rel)
    assert s["en"]["recall@1"] == 1.0 and s["hi"]["recall@1"] == 0.0 and s["hi"]["recall@3"] == 1.0
    assert s["all"]["mrr"] == 0.75 and s["all"]["queries"] == 2


def test_samples_and_labels_cover_every_concept_in_every_language():
    samples = bm.load_samples()
    rel = bm.relevant_docs(samples)
    assert len(samples) == 20 and all(len(v) >= 2 for v in rel.values()), {k: len(v) for k, v in rel.items()}
    assert {lang for _, qs in bm.CONCEPTS.values() for lang in qs} == {"en", "hi", "ar"}


def test_the_estimate_is_computed_from_the_actual_texts_and_is_tiny():
    est = bm.estimate(bm.load_samples())
    assert est["provider"] == "nebius" and est["queries"] == 18 and est["texts"] == est["chunks"] + 18
    assert est["documents"] == 20 and est["estimated_tokens"] > 1000
    assert (
        Decimal("0") < Decimal(est["estimated_cost_usd"]) < Decimal("0.001")
    )  # a fraction of a cent of the $50 credit


def test_the_benchmark_never_spends_without_the_opt_in_and_an_approval(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("a router was built: nothing may be spent")

    monkeypatch.setattr(service, "make_router", boom)
    assert bm.main([]) == 0  # default: estimate only
    report = json.loads(capsys.readouterr().out)
    assert report["ran"] is False and report["estimate"]["texts"] > 0
    monkeypatch.setattr(bm, "get_ai_settings", lambda: AiSettings(_env_file=None))  # Nebius off
    with pytest.raises(SystemExit, match="opt-in"):
        bm.main(["--run", "--approve-cost-usd", "1"])
    monkeypatch.setattr(
        bm, "get_ai_settings", lambda: AiSettings(_env_file=None, anticipa_enable_nebius=True)
    )
    with pytest.raises(SystemExit, match="approval required"):
        bm.main(["--run"])
    with pytest.raises(SystemExit, match="approval required"):
        bm.main(["--run", "--approve-cost-usd", "0.000000"])  # below the estimate


# a keyword-feature "embedding": one dimension per concept, so the semantic ranker is exercised end to end offline
_FEATURES = [
    ("breast", "स्तन", "الثدي"), ("diabet", "मधुमेह", "السكري"), ("colonoscop", "कोलोनोस्कोपी", "القولون"),
    ("smok", "tobacco", "धूम्रपान", "التدخين", "cigarette", "तंबाकू"), ("glucose", "ग्लूकोज", "سكر الدم"), ("lump", "गाँठ", "كتلة"),
]  # fmt: skip


class KeywordEmbedder(FakeAdapter):
    async def embed(self, model, texts, dimensions):
        out = []
        for t in texts:
            v = [0.0] * dimensions
            for i, words in enumerate(_FEATURES):
                v[i] = 1.0 if any(w in t.lower() for w in words) else 0.0
            v[dimensions - 1] = 0.01
            out.append(v)
        return out, Usage(input_tokens=sum(len(t) for t in texts) // 3)


@pytest.mark.anyio
async def test_semantic_ranker_end_to_end_with_a_fake_provider():
    samples = bm.load_samples()
    router, sink, _ = make_test_router({"nebius": KeywordEmbedder()})
    rankings, cost, tokens = await bm.semantic_rankings(router, samples)
    assert (
        len(rankings) == 18
        and tokens > 0
        and cost == sum(r.cost_usd for r in sink.records) > 0
        and len(sink.records) == 2
    )
    s = bm.summarise(rankings, bm.relevant_docs(samples))
    assert s["all"]["recall@5"] > 0.4 and s["all"]["mrr"] > 0.8 and s["en"]["queries"] == 6


@pytest.mark.integration
def test_fulltext_ranker_finds_same_language_documents(sessions):
    samples = bm.load_samples()
    with sessions() as s:
        rankings = bm.fulltext_rankings(s, samples)
    by_key = {x.key: x for x in samples}
    en_top = rankings[("diabetes", "en")][0]
    assert "diabetes" in by_key[en_top].text.lower()
    for concept, lang in (("diabetes", "hi"), ("breast_cancer", "ar"), ("glucose", "hi")):
        top = rankings[(concept, lang)]
        assert top and by_key[top[0]].language == lang, (
            concept,
            lang,
        )  # 'simple' tokenises Devanagari and Arabic
    rel = bm.relevant_docs(samples)
    s = bm.summarise(rankings, rel)
    assert s["en"]["mrr"] > 0.8 and 0 < s["hi"]["recall@5"] <= 1
