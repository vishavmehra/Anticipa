# ruff: noqa: E501  (long docstring/comment lines)
"""Live provider smoke tests (paid, opt-in: `uv run --directory backend pytest -m live tests/ai/test_live.py -s`).

Budget: one short English and one short Hindi synthetic note through each provider (Nebius Qwen3-235B and Claude
Haiku), one embedding call, one JSON-schema probe. Expected total well under $0.10; the router's own spend guard is
set to $0.10 for this module, so the suite cannot overspend. Synthetic text only, no real patient data.
"""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from pathlib import Path

import pytest

from anticipa.ai.config import Candidate, RouterConfig, load_config
from anticipa.ai.router import build_router
from anticipa.ai.settings import AiSettings
from anticipa.ai.sinks import InMemorySink
from anticipa.ai.types import AdapterCall, EmbedRequest
from anticipa.evidence import DocumentMeta, extract_document

pytestmark = pytest.mark.live

NOTE_EN = (
    "Synthetic clinical note. The patient is a 52-year-old woman attending a routine review. "
    "Her mother was diagnosed with breast cancer at age 45. The patient denies tobacco use. "
    "Colonoscopy was advised for next year."
)
NOTE_HI = (
    "कृत्रिम नैदानिक नोट। मरीज़ 52 वर्ष की महिला हैं। उनकी माँ को 45 वर्ष की आयु में स्तन कैंसर का निदान हुआ था। "
    "मरीज़ को मधुमेह नहीं है।"
)
BUDGET = Decimal("0.10")
REPORT: list[dict] = []


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def _only(cfg: RouterConfig, task: str, provider: str, model_id: str) -> RouterConfig:
    out = cfg.model_copy(deep=True)
    out.tasks[task].candidates = [Candidate(provider=provider, model_id=model_id)]
    return out


def _router(cfg: RouterConfig | None = None):
    settings = AiSettings()
    if not (settings.anthropic_api_key and settings.nebius_api_key):
        pytest.skip("provider keys are not configured in .env")
    sink = InMemorySink()
    router = build_router(settings=settings, config=cfg, sink=sink)
    router.daily_budget_usd = BUDGET
    return router, sink


def _note(label: str, **kw) -> None:
    REPORT.append({"label": label, **kw})
    print("LIVE", json.dumps(kw, default=str, ensure_ascii=False))  # noqa: T201


CASES = [
    ("nebius", "Qwen/Qwen3-235B-A22B-Instruct-2507"),
    ("anthropic", "claude-haiku-5-5"),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("provider", "model_id"), CASES)
@pytest.mark.parametrize(("lang", "text"), [("en", NOTE_EN), ("hi", NOTE_HI)])
async def test_extraction_live(provider, model_id, lang, text):
    router, sink = _router(_only(load_config(), "evidence.extract", provider, model_id))
    try:
        out = await extract_document(router, text, DocumentMeta(document_id=f"live-{lang}", language=lang))
    finally:
        await router.aclose()
    assert out.status == "completed", (out.error_category, out.error_message)
    assert out.provider == provider and out.model_id == model_id and not out.fallback_used
    _note(
        f"extract/{provider}/{lang}",
        provider=provider,
        model=model_id,
        lang=lang,
        input_tokens=out.usage.input_tokens,
        output_tokens=out.usage.output_tokens,
        cache_read=out.usage.cache_read_tokens,
        cache_write=out.usage.cache_write_tokens,
        cost_usd=str(out.cost_usd),
        latency_ms=out.latency_ms,
        repairs=out.repairs,
        retries=out.retries,
        facts=len(out.facts),
        accepted=out.count("accepted"),
        requires_review=out.count("requires_review"),
        rejected=out.count("rejected"),
    )
    family = [
        f
        for f in out.facts
        if f.candidate.subject_type == "family_member"
        and f.candidate.relationship == "mother"
        and "breast" in f.candidate.value.concept.lower()
    ]
    assert family, "the planted 'mother had breast cancer' fact was not extracted"
    assert all(f.verification.verified for f in family), "its quote must verify against the original text"
    assert family[0].candidate.assertion_status == "positive"


@pytest.mark.anyio
async def test_embedding_live_is_1024_dimensional():
    router, _ = _router()
    try:
        res = await router.embed(EmbedRequest(task="embedding.document", texts=[NOTE_EN, NOTE_HI]))
    finally:
        await router.aclose()
    assert res.dimensions == 1024 and len(res.vectors) == 2 and all(len(v) == 1024 for v in res.vectors)
    _note(
        "embed",
        model=res.model_id,
        input_tokens=res.usage.input_tokens,
        cost_usd=str(res.cost_usd),
        latency_ms=res.latency_ms,
        dims=len(res.vectors[0]),
    )


@pytest.mark.anyio
async def test_nebius_honours_json_schema_response_format():
    """Adversarial probe: the prompt asks for a value the schema forbids. Constrained decoding must win."""
    router, _ = _router()
    adapter = router.adapters["nebius"]
    model = router.config.model("nebius", "Qwen/Qwen3-235B-A22B-Instruct-2507")
    assert model.structured_output == "json_schema"
    schema = {
        "type": "object",
        "properties": {"color": {"type": "string", "enum": ["red", "green"]}},
        "required": ["color"],
        "additionalProperties": False,
    }
    results = []
    try:
        for prompt in ("Answer with the colour blue.", "What colour is the sky? Answer in prose, not JSON."):
            started = time.perf_counter()
            resp = await adapter.generate(
                AdapterCall(
                    model=model,
                    system="",
                    messages=[{"role": "user", "content": prompt}],
                    schema=schema,
                    max_output_tokens=60,
                    temperature=0.0,
                )
            )
            results.append(
                (resp.text, resp.usage, round((time.perf_counter() - started) * 1000), model.cost(resp.usage))
            )
    finally:
        await router.aclose()
    for text, _usage, _ms, _cost in results:
        assert json.loads(text)["color"] in {"red", "green"}, text
    _note(
        "nebius_json_schema_probe",
        honoured=True,
        outputs=[r[0] for r in results],
        input_tokens=sum(r[1].input_tokens for r in results),
        output_tokens=sum(r[1].output_tokens for r in results),
        cost_usd=str(sum(r[3] for r in results)),
        latency_ms=[r[2] for r in results],
    )


def test_zz_total_live_cost_is_within_budget():
    total = sum(Decimal(r["cost_usd"]) for r in REPORT if "cost_usd" in r)
    print("LIVE total_cost_usd", total)  # noqa: T201
    assert total < BUDGET
    out = os.environ.get("ANTICIPA_LIVE_REPORT")
    if out:
        Path(out).write_text(json.dumps(REPORT, indent=2, ensure_ascii=False), encoding="utf-8")
