"""Cost estimates computed BEFORE any paid call (owner policy 2026-11-11). Pure functions, no I/O, no
provider call.

Extraction (Claude): per-document token profiles measured live on 20 synthetic en/hi/ar documents on
2026-11-11
(tests/evidence/test_live_measure.py), priced from config/models.toml so a price change flows through.
Embeddings (Nebius): tokens are estimated from the ACTUAL selected texts with a script-aware heuristic (no
Qwen tokenizer is installed); it is deliberately conservative, so the real bill is at or below the estimate.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any

from anticipa.ai.config import RouterConfig
from anticipa.ai.types import Usage

ESCALATION_SHARE = Decimal("0.10")  # hard cap: escalated documents / cohort documents

# Mean tokens per document, measured (cache_read/cache_write reflect the prompt-cache behaviour seen live).
# Haiku: calibrated on 18 real docgen documents ($0.000384 measured; the profile prices at $0.000391 with the
# system prompt served from the prompt cache). Sonnet: calibrated on 8 escalated documents (6 samples +
# 2 docgen),
# $0.0225-$0.0268 per escalation; output tokens set so the profile prices at the higher end ($0.0269).
HAIKU_PROFILE = Usage(input_tokens=269, output_tokens=650, cache_read_tokens=3952, cache_write_tokens=0)
SONNET_PROFILE = Usage(input_tokens=286, output_tokens=2100, cache_read_tokens=1976, cache_write_tokens=1976)
UPPER_FACTOR = 3  # "upper" scenario: documents three times longer (input and output scale)

_USD = Decimal("0.0001")


def _scaled(u: Usage, factor: int) -> Usage:
    return Usage(
        input_tokens=u.input_tokens * factor,
        output_tokens=u.output_tokens * factor,
        cache_read_tokens=u.cache_read_tokens,
        cache_write_tokens=u.cache_write_tokens,
    )


def escalation_cap(
    cohort_documents: int, already_escalated: int = 0, share: Decimal = ESCALATION_SHARE
) -> int:
    """Escalations still allowed: floor(share of the cohort's documents) minus those already used."""
    return max(int(cohort_documents * min(share, ESCALATION_SHARE)) - already_escalated, 0)


def estimate_extraction(
    config: RouterConfig, documents: int, cap: int, *, run_budget: Decimal | None = None
) -> dict[str, Any]:
    """Expected and upper-bound Claude cost of an extraction run (nothing cached, escalation cap fully
    used)."""
    haiku = config.model("anthropic", "claude-haiku-5-5")
    sonnet = config.model("anthropic", "claude-sonnet-5-5")
    escalations = min(cap, documents)
    expected_haiku = haiku.cost(HAIKU_PROFILE) * documents
    expected_sonnet = sonnet.cost(SONNET_PROFILE) * escalations
    upper_haiku = haiku.cost(_scaled(HAIKU_PROFILE, UPPER_FACTOR)) * documents
    upper_sonnet = sonnet.cost(_scaled(SONNET_PROFILE, UPPER_FACTOR)) * escalations
    expected, upper = expected_haiku + expected_sonnet, upper_haiku + upper_sonnet
    return {
        "documents": documents,
        "max_escalations": escalations,
        "expected_haiku_usd": str(expected_haiku.quantize(_USD, ROUND_HALF_UP)),
        "expected_sonnet_usd": str(expected_sonnet.quantize(_USD, ROUND_HALF_UP)),
        "expected_total_usd": str(expected.quantize(_USD, ROUND_HALF_UP)),
        "upper_total_usd": str(upper.quantize(_USD, ROUND_HALF_UP)),
        "per_1000_documents_usd": str(
            ((expected / documents) * 1000).quantize(_USD, ROUND_HALF_UP) if documents else Decimal(0)
        ),
        "assumes": "nothing cached; every allowed escalation used; upper = documents 3x longer",
        "run_budget_usd": str(run_budget) if run_budget is not None else None,
        "within_run_budget": run_budget is None or expected <= run_budget,
    }


# ------------------------------------------------------------------------------------------------ embeddings
# Devanagari, Arabic (+ supplement, presentation forms): ~1 token per character (measured live: 196
# estimated vs 172 billed on one en+hi pair)
# against ~4.5 for English.
_DENSE_RANGES = ((0x0900, 0x097F), (0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: 1.0 token per Devanagari/Arabic character, 0.3 per other character."""
    dense = sum(1 for ch in text if any(lo <= ord(ch) <= hi for lo, hi in _DENSE_RANGES))
    return math.ceil(dense * 1.0 + (len(text) - dense) * 0.3)


def estimate_embedding(
    config: RouterConfig, texts: list[str], *, task: str = "embedding.document"
) -> dict[str, Any]:
    """Tokens and USD for embedding exactly these texts (the unique ones still missing an embedding)."""
    route = config.tasks[task]
    model = config.model(route.candidates[0].provider, route.candidates[0].model_id)
    tokens = sum(estimate_tokens(t) for t in texts)
    # rounded UP: an estimate must never show $0.000000 for a non-empty run (approval is compared against it)
    cost = (Decimal(tokens) * model.input_per_mtok / Decimal(1_000_000)).quantize(
        Decimal("0.000001"), ROUND_UP
    )
    return {
        "provider": model.provider,
        "model": model.id,
        "dimensions": route.dimensions,
        "texts": len(texts),
        "estimated_tokens": tokens,
        "estimated_cost_usd": str(cost),
        "price_per_mtok_usd": str(model.input_per_mtok),
    }
