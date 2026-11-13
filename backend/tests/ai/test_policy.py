"""Owner credit policy (2026-11-11): Nebius is opt-in and never an implicit route, per-run budget cap,
Nebius credit
ledger with a hard stop, health that never calls a disabled provider."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import BaseModel

from anticipa.ai.health import check_providers
from anticipa.ai.settings import AiSettings
from anticipa.ai.sinks import InMemorySink, InvocationRecord
from anticipa.ai.testing import FakeAdapter, err, make_test_router, ok, two_provider_config
from anticipa.ai.types import EmbedRequest, GenerateRequest, RouterError

pytestmark = pytest.mark.anyio
D = Decimal
GOOD = json.dumps({"name": "x", "n": 1})


class Fact(BaseModel):
    name: str
    n: int


def req(task="evidence.extract", **kw) -> GenerateRequest:
    base = dict(
        task=task,
        system="sys",
        messages=[{"role": "user", "content": "doc"}],
        output_schema=Fact,
        prompt_version="t@1",
    )
    base.update(kw)
    return GenerateRequest(**base)


# ------------------------------------------------------------------------------------------- Nebius gate
async def test_with_nebius_disabled_it_is_never_a_candidate_not_even_as_fallback():
    nebius, haiku = (
        FakeAdapter(ok(GOOD)),
        FakeAdapter(err("unavailable"), err("unavailable"), err("unavailable")),
    )
    router, _, _ = make_test_router(
        {"nebius": nebius, "anthropic": haiku},
        config=two_provider_config(),
        enabled_providers=frozenset({"anthropic"}),
    )
    with pytest.raises(RouterError) as ei:  # the route lists Nebius first, then Haiku: only Haiku remains
        await router.generate(req())
    assert ei.value.category == "unavailable" and nebius.calls == [] and len(haiku.calls) == 3


async def test_embeddings_have_no_route_when_nebius_is_disabled():
    nebius = FakeAdapter(ok("", i=10, o=0))
    router, sink, _ = make_test_router({"nebius": nebius}, enabled_providers=frozenset({"anthropic"}))
    with pytest.raises(RouterError, match="ANTICIPA_ENABLE_NEBIUS") as ei:
        await router.embed(EmbedRequest(task="embedding.document", texts=["x"]))
    assert ei.value.category == "invalid_request" and nebius.embed_calls == [] and sink.records == []


async def test_shipped_routes_are_haiku_and_sonnet_only():
    claude = FakeAdapter(ok(GOOD), ok(GOOD), ok(GOOD))
    router, sink, _ = make_test_router({"anthropic": claude}, enabled_providers=frozenset({"anthropic"}))
    for task in ("evidence.extract", "evidence.extract.complex", "engagement.draft"):
        res = await router.generate(req(task))
        assert res.provider == "anthropic" and not res.fallback_used
    assert [r.model_id for r in sink.records] == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]


async def test_extraction_has_no_fallback_a_failed_haiku_just_fails():
    nebius = FakeAdapter(ok(GOOD))
    haiku = FakeAdapter(err("rate_limited"), err("rate_limited"), err("rate_limited"))
    router, sink, sleeps = make_test_router(
        {"anthropic": haiku, "nebius": nebius}
    )  # shipped config, all enabled
    with pytest.raises(RouterError) as ei:
        await router.generate(req())
    assert (
        ei.value.category == "rate_limited" and nebius.calls == [] and len(haiku.calls) == 3
    )  # 1 + 2 retries
    assert len(sleeps) == 2 and sink.records[0].status == "failed"


async def test_health_never_probes_a_disabled_provider():
    s = AiSettings(_env_file=None, anthropic_api_key=None, nebius_api_key="n-x")
    out = await check_providers(s)
    assert out["nebius"].status == "disabled" and "ANTICIPA_ENABLE_NEBIUS" in (out["nebius"].detail or "")
    assert out["anthropic"].status == "not_configured"


# ------------------------------------------------------------------------------------------- run budget
async def test_run_budget_caps_the_sum_of_one_runs_calls_and_survives_a_resume():
    claude = FakeAdapter(*[ok(GOOD, i=1000, o=500) for _ in range(10)])
    one_call = D("0.00035")  # Haiku: 1000 in * $0.10/M + 500 out * $0.50/M
    router, _, _ = make_test_router({"anthropic": claude}, run_budget_usd=D("0.0030"))
    done = 0
    with pytest.raises(RouterError) as ei:
        for _ in range(10):
            await router.generate(req())
            done += 1
    assert ei.value.category == "budget_exceeded" and "per-run" in ei.value.message
    assert 1 <= done < 10 and router.run_spent_usd <= D("0.0030") and len(claude.calls) == done
    assert router.run_spent_usd == one_call * done  # only completed calls are billed; the refused one is not

    # a resumed run starts from what the earlier attempt already spent
    from anticipa.ai.router import ModelRouter

    resumed = ModelRouter(
        router.config,
        {"anthropic": FakeAdapter(ok(GOOD))},
        InMemorySink(),
        run_budget_usd=D("0.01"),
        initial_run_spend=D("0.00999"),
    )
    with pytest.raises(RouterError, match="per-run"):
        await resumed.generate(req())


async def test_a_cache_hit_costs_nothing_against_the_run_budget(tmp_path):
    from anticipa.ai.cache import ResponseCache

    claude = FakeAdapter(ok(GOOD, i=1000, o=500))
    router, _, _ = make_test_router(
        {"anthropic": claude}, cache=ResponseCache(tmp_path), run_budget_usd=D("0.01")
    )
    first = await router.generate(req())
    for _ in range(20):  # a rerun replays; the run budget is not touched and the provider is not called
        assert (await router.generate(req())).cached
    assert len(claude.calls) == 1 and router.run_spent_usd == first.cost_usd


# ------------------------------------------------------------------------------------ Nebius credit ledger
def nebius_spend(cost: str) -> InvocationRecord:
    return InvocationRecord(
        id="a" * 32,
        request_id="r",
        task="embedding.document",
        provider="nebius",
        model_id="Qwen/Qwen3-Embedding-8B",
        prompt_version="n/a",
        cost_usd=D(cost),
    )


async def test_nebius_credit_ledger_hard_stops_before_any_request():
    sink = InMemorySink()
    await sink.record(nebius_spend("49.9999999"))  # lifetime Nebius spend recorded in model_invocations
    nebius = FakeAdapter(ok("", i=500_000, o=0))
    router, _, _ = make_test_router({"nebius": nebius}, sink=sink, provider_credit_usd={"nebius": D("50")})
    with pytest.raises(RouterError) as ei:
        await router.embed(EmbedRequest(task="embedding.document", texts=["x" * 4000]))
    assert (
        ei.value.category == "budget_exceeded"
        and "nebius credit" in ei.value.message
        and nebius.embed_calls == []
    )


async def test_nebius_credit_ledger_ignores_claude_spend_and_allows_small_calls():
    sink = InMemorySink()
    await sink.record(
        InvocationRecord(
            id="b" * 32,
            request_id="r",
            task="t",
            provider="anthropic",
            model_id="m",
            prompt_version="p",
            cost_usd=D("99"),
        )
    )
    nebius = FakeAdapter(ok("", i=100, o=0))
    router, _, _ = make_test_router({"nebius": nebius}, sink=sink, provider_credit_usd={"nebius": D("50")})
    res = await router.embed(EmbedRequest(task="embedding.document", texts=["hello"]))
    assert res.cost_usd > 0 and len(nebius.embed_calls) == 1
    assert (
        await sink.spent_total_usd("nebius") == res.cost_usd
        and await sink.spent_total_usd() == D("99") + res.cost_usd
    )


async def test_an_approved_run_limit_stops_a_nebius_run_mid_way():
    nebius = FakeAdapter(*[ok("", i=10_000, o=0) for _ in range(5)])  # $0.0001 per call at $0.01/Mtok
    router, _, _ = make_test_router({"nebius": nebius}, run_budget_usd=D("0.00025"))
    calls = 0
    with pytest.raises(RouterError) as ei:
        for _ in range(5):
            await router.embed(EmbedRequest(task="embedding.document", texts=["x" * 20_000]))
            calls += 1
    assert ei.value.category == "budget_exceeded" and calls == 2 and len(nebius.embed_calls) == 2


async def test_anthropic_lifetime_cap_hard_stops_before_any_request():
    sink = InMemorySink()
    await sink.record(
        InvocationRecord(id="d" * 32, request_id="r", task="t", provider="anthropic", model_id="m",
                         prompt_version="p", cost_usd=D("79.9995"))  # fmt: skip
    )
    claude = FakeAdapter(ok(GOOD, i=1000, o=500))
    router, _, _ = make_test_router({"anthropic": claude}, sink=sink, provider_credit_usd={"anthropic": D("80")})
    with pytest.raises(RouterError) as ei:  # worst case of one extraction call is a few thousandths of a dollar
        await router.generate(req())
    assert ei.value.category == "budget_exceeded" and "anthropic credit" in ei.value.message and claude.calls == []


def test_build_router_wires_the_anthropic_cap_only_when_configured():
    from anticipa.ai.router import build_router

    base = {"_env_file": None, "anthropic_api_key": "sk-ant-x"}
    assert "anthropic" not in build_router(settings=AiSettings(**base)).provider_credit_usd
    capped = build_router(settings=AiSettings(**base, anticipa_anthropic_spend_cap_usd="80"))
    assert capped.provider_credit_usd == {"nebius": D("50.00"), "anthropic": D("80")}
