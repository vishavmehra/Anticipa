"""Retrieval benchmark: PostgreSQL full-text search vs Nebius semantic search on English / Hindi /
Arabic queries over
the 20 labelled synthetic samples (recall@k and MRR, relevance defined by the planted concept, so cross-
language
retrieval counts).

    uv run --directory backend python -m anticipa.evidence.benchmark                       # estimate
    only (default)
    ANTICIPA_ENABLE_NEBIUS=1 uv run --directory backend python -m anticipa.evidence.benchmark --run
    --approve-cost-usd 0.01

It is NOT run by the test suite and spends nothing unless `--run` is given together with an approval
amount that is
at least the estimate, Nebius is opted in (ANTICIPA_ENABLE_NEBIUS=1) and the credit ledger has room. The
full-text
side needs the local database (read-only `SELECT to_tsvector(...)` on passed-in text); the semantic side
embeds the
sample chunks and the queries through the Model Router (hard-stopped at the approved amount) and ranks
by cosine.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from anticipa.ai import EmbedRequest
from anticipa.ai.config import load_config
from anticipa.ai.settings import get_ai_settings
from anticipa.evidence import service
from anticipa.evidence.chunking import chunk_text
from anticipa.evidence.estimate import estimate_embedding
from anticipa.evidence.routes import (  # the product search's own queries: same behaviour measured
    _FTS_AND,
    _FTS_OR,
)

SAMPLES_FILE = Path(__file__).resolve().parents[2] / "tests" / "evidence" / "evidence_samples.py"
# concept -> (relevance regex over the planted facts, query per language)
CONCEPTS: dict[str, tuple[str, dict[str, str]]] = {
    "breast_cancer": ("breast", {"en": "breast cancer in the family", "hi": "स्तन कैंसर", "ar": "سرطان الثدي"}),
    "diabetes": ("diabetes", {"en": "diabetes", "hi": "मधुमेह", "ar": "داء السكري"}),
    "colonoscopy": ("colonoscopy", {"en": "colonoscopy", "hi": "कोलोनोस्कोपी", "ar": "تنظير القولون"}),
    "smoking": ("smok|tobacco|cigarette", {"en": "smoking tobacco", "hi": "धूम्रपान", "ar": "التدخين"}),
    "glucose": ("glucose", {"en": "blood glucose", "hi": "ग्लूकोज", "ar": "سكر الدم"}),
    "lump": ("lump|mass", {"en": "breast lump", "hi": "स्तन में गाँठ", "ar": "كتلة في الثدي"}),
}


def load_samples() -> list[Any]:
    spec = importlib.util.spec_from_file_location("evidence_samples", SAMPLES_FILE)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules.setdefault("evidence_samples", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return list(mod.SAMPLES)


def relevant_docs(samples: list[Any]) -> dict[str, set[str]]:
    return {
        c: {s.key for s in samples if any(re.search(rx, e[0]) for e in s.expect)}
        for c, (rx, _) in CONCEPTS.items()
    }


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant) if relevant else 0.0


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    return next((1 / (i + 1) for i, d in enumerate(ranked) if d in relevant), 0.0)


def cosine(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb) if na and nb else 0.0


def doc_ranking(chunk_scores: list[tuple[str, float]]) -> list[str]:
    """Best chunk score per document, documents ordered best first (ties by key: deterministic)."""
    best: dict[str, float] = {}
    for key, score in chunk_scores:
        best[key] = max(score, best.get(key, float("-inf")))
    return [k for k, _ in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))]


def summarise(
    rankings: dict[tuple[str, str], list[str]], rel: dict[str, set[str]], ks=(1, 3, 5)
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for lang in ("en", "hi", "ar", "all"):
        rows = [(c, r) for (c, lg), r in rankings.items() if lang in (lg, "all")]
        if not rows:
            continue
        out[lang] = {
            **{
                f"recall@{k}": round(sum(recall_at_k(r, rel[c], k) for c, r in rows) / len(rows), 3)
                for k in ks
            },
            "mrr": round(sum(reciprocal_rank(r, rel[c]) for c, r in rows) / len(rows), 3),
            "queries": len(rows),
        }
    return out


def fulltext_rankings(session: Any, samples: list[Any]) -> dict[tuple[str, str], list[str]]:
    cuts = [(s.key, c.text) for s in samples for c in chunk_text(s.text)]
    texts = [t for _, t in cuts]
    out = {}
    for concept, (_, queries) in CONCEPTS.items():
        for lang, q in queries.items():
            params = {"texts": texts, "q": q, "limit": len(texts)}
            rows = (
                session.execute(_FTS_AND, params).all() or session.execute(_FTS_OR, params).all()
            )  # as in search
            out[(concept, lang)] = doc_ranking([(cuts[r.i - 1][0], float(r.rank)) for r in rows])
    return out


def benchmark_texts(samples: list[Any]) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    chunk_texts = [c.text for s in samples for c in chunk_text(s.text)]
    keys = [(c, lang) for c, (_, qs) in CONCEPTS.items() for lang in qs]
    return chunk_texts, keys, [CONCEPTS[c][1][lang] for c, lang in keys]


def estimate(samples: list[Any]) -> dict[str, Any]:
    chunks, _, queries = benchmark_texts(samples)
    est = estimate_embedding(load_config(), chunks + queries)
    est.update(documents=len(samples), chunks=len(chunks), queries=len(queries), calls=2)
    return est


async def semantic_rankings(
    router: Any, samples: list[Any]
) -> tuple[dict[tuple[str, str], list[str]], Decimal, int]:
    chunk_pairs = [(s.key, c.text) for s in samples for c in chunk_text(s.text)]
    _, keys, queries = benchmark_texts(samples)
    docs = await router.embed(EmbedRequest(task="embedding.document", texts=[t for _, t in chunk_pairs]))
    qs = await router.embed(EmbedRequest(task="embedding.document", texts=queries))
    out = {
        key: doc_ranking([(d[0], cosine(qv, dv)) for d, dv in zip(chunk_pairs, docs.vectors, strict=True)])
        for key, qv in zip(keys, qs.vectors, strict=True)
    }
    return out, docs.cost_usd + qs.cost_usd, docs.usage.input_tokens + qs.usage.input_tokens


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true", help="actually call Nebius (needs --approve-cost-usd)")
    ap.add_argument(
        "--approve-cost-usd", type=Decimal, default=None, help="approved spend; must be >= the estimate"
    )
    args = ap.parse_args(argv)
    samples = load_samples()
    est = estimate(samples)
    report: dict[str, Any] = {"estimate": est, "ran": False}
    if args.run:
        settings = get_ai_settings()
        need = Decimal(est["estimated_cost_usd"])
        approved = (
            args.approve_cost_usd
            if args.approve_cost_usd is not None
            else settings.anticipa_nebius_run_approved_usd
        )
        if "nebius" not in settings.enabled_providers:
            sys.exit("refused: Nebius is opt-in; set ANTICIPA_ENABLE_NEBIUS=1")
        if approved is None or approved < need:
            sys.exit(f"refused: approval required, estimate is ${need} (pass --approve-cost-usd >= that)")
        from anticipa.db import get_sessionmaker

        sessions = get_sessionmaker()
        router = service.make_router(sessions, run_budget=approved)  # hard stop at the approved amount
        rel = relevant_docs(samples)
        with sessions() as s:
            fts = summarise(fulltext_rankings(s, samples), rel)

        async def go() -> Any:
            try:
                return await semantic_rankings(router, samples)
            finally:
                await router.aclose()

        rankings, cost, tokens = asyncio.run(go())
        report.update(
            ran=True, fulltext=fts, semantic=summarise(rankings, rel),
            measured_cost_usd=str(cost), measured_tokens=tokens, approved_usd=str(approved),
        )  # fmt: skip
    print(json.dumps(report, indent=2, ensure_ascii=False))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
