"""Daily spend guard (ANTICIPA_AI_DAILY_BUDGET_USD): refuses provider calls whose worst-case cost would
push today's UTC spend over budget, counting in-flight calls and unrecorded cost of the current attempt."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel

from anticipa.ai.sinks import InMemorySink, InvocationRecord
from anticipa.ai.testing import FakeAdapter, err, make_test_router, ok
from anticipa.ai.types import EmbedRequest, GenerateRequest, RouterError

pytestmark = pytest.mark.anyio
D = Decimal


class Fact(BaseModel):
    name: str
    n: int


GOOD = json.dumps({"name": "x", "n": 1})


def req(**kw) -> GenerateRequest:
    base = dict(
        task="evidence.extract",
        system="sys",
        messages=[{"role": "user", "content": "doc text"}],
        output_schema=Fact,
        prompt_version="t@1",
    )
    base.update(kw)
    return GenerateRequest(**base)


def spend(cost: str, *, when: datetime | None = None) -> InvocationRecord:
    rec = InvocationRecord(
        id="a" * 32, request_id="r", task="t", provider="nebius", model_id="m", prompt_version="p",
        cost_usd=D(cost),
    )  # fmt: skip
    if when:
        rec.created_at = when
    return rec


async def test_under_budget_passes_and_guard_off_by_default():
    router, _, _ = make_test_router({"nebius": FakeAdapter(ok(GOOD))}, daily_budget_usd=D("5.00"))
    assert (await router.generate(req())).output.n == 1
    unguarded, sink, _ = make_test_router({"nebius": FakeAdapter(ok(GOOD))})  # no budget -> no guard
    await sink.record(spend("1000"))
    assert (await unguarded.generate(req())).output.n == 1


async def test_over_budget_refuses_before_calling_any_provider_and_never_falls_back():
    nebius, claude = FakeAdapter(ok(GOOD)), FakeAdapter(ok(GOOD))
    sink = InMemorySink()
    await sink.record(spend("4.999"))
    router, _, sleeps = make_test_router(
        {"nebius": nebius, "anthropic": claude}, sink=sink, daily_budget_usd=D("5")
    )
    with pytest.raises(RouterError) as ei:
        await router.generate(req())  # worst case ~ $0.0024 (4000 output tokens at $0.60/M) > $0.001 left
    e = ei.value
    assert e.category == "budget_exceeded" and not e.retryable and "$5" in e.message
    assert nebius.calls == [] and claude.calls == [] and sleeps == []
    assert len(sink.records) == 1  # no accounting row per refused call (no write amplification)


async def test_worst_case_uses_max_output_tokens():
    sink = InMemorySink()
    await sink.record(spend("4.9985"))  # $0.0015 left
    router, _, _ = make_test_router({"nebius": FakeAdapter(ok(GOOD))}, sink=sink, daily_budget_usd=D("5"))
    with pytest.raises(RouterError) as ei:
        await router.generate(req(max_output_tokens=4000))  # 4000 * 0.60/M = 0.0024
    assert ei.value.category == "budget_exceeded"
    assert (await router.generate(req(max_output_tokens=200))).output.n == 1  # 200 * 0.60/M = 0.00012


async def test_only_todays_utc_spend_counts():
    now = datetime.now(UTC)
    sink = InMemorySink()
    await sink.record(spend("100", when=now - timedelta(days=1)))  # yesterday: ignored
    router, _, _ = make_test_router({"nebius": FakeAdapter(ok(GOOD))}, sink=sink, daily_budget_usd=D("5"))
    assert (await router.generate(req())).output.n == 1
    assert await sink.spent_today_usd() == D("0.000050")  # only the new call; yesterday's $100 is excluded


async def test_clock_crossing_utc_midnight_resets_spend():
    t = {"now": datetime(2026, 11, 6, 23, 59, tzinfo=UTC)}
    sink = InMemorySink(now=lambda: t["now"])
    await sink.record(spend("4.99", when=t["now"]))
    assert await sink.spent_today_usd() == D("4.99")
    t["now"] = datetime(2026, 11, 7, 0, 1, tzinfo=UTC)
    assert await sink.spent_today_usd() == D("0")


async def test_repair_turn_is_guarded_and_first_turn_cost_is_not_lost():
    nebius, claude = FakeAdapter(ok("not json", i=1000, o=500), ok(GOOD)), FakeAdapter(ok(GOOD))
    # one call's worst case is ~$0.00242; the budget fits one such call but not first-turn cost + a second
    router, sink, _ = make_test_router({"nebius": nebius, "anthropic": claude}, daily_budget_usd=D("0.0028"))
    with pytest.raises(RouterError) as ei:
        await router.generate(req())
    assert ei.value.category == "budget_exceeded"
    assert len(nebius.calls) == 1 and claude.calls == []  # repair blocked, no fallback
    [rec] = sink.records  # the first turn really cost money, so it is recorded
    assert (
        rec.status == "failed" and rec.error_category == "budget_exceeded" and rec.cost_usd == D("0.000500")
    )
    assert router._reserved == 0  # nothing leaks from the in-flight ledger


async def test_concurrent_calls_see_each_others_reservations():
    class Gated(FakeAdapter):
        gate = asyncio.Event()

        async def generate(self, call):
            await self.gate.wait()
            return await super().generate(call)

    adapter = Gated(ok(GOOD))
    router, _, _ = make_test_router(
        {"nebius": adapter}, daily_budget_usd=D("0.004")
    )  # fits one call, not two
    first = asyncio.create_task(router.generate(req()))
    for _ in range(5):
        await asyncio.sleep(0)  # let the first call reserve and park at the gate
    with pytest.raises(RouterError) as ei:
        await router.generate(req())
    assert ei.value.category == "budget_exceeded"
    adapter.gate.set()
    assert (await first).output.n == 1
    assert router._reserved == 0


async def test_unreadable_spend_fails_closed():
    class BrokenSink(InMemorySink):
        async def spent_today_usd(self):
            raise RuntimeError("db down")

    nebius = FakeAdapter(ok(GOOD))
    router, _, _ = make_test_router({"nebius": nebius}, sink=BrokenSink(), daily_budget_usd=D("5"))
    with pytest.raises(RouterError, match="could not be verified") as ei:
        await router.generate(req())
    assert ei.value.category == "budget_exceeded" and nebius.calls == []


async def test_embeddings_are_guarded_too():
    nebius = FakeAdapter(ok("", i=10, o=0))
    router, _, _ = make_test_router({"nebius": nebius}, daily_budget_usd=D("0"))
    with pytest.raises(RouterError) as ei:
        await router.embed(EmbedRequest(texts=["some text to embed"]))
    assert ei.value.category == "budget_exceeded" and nebius.embed_calls == []


async def test_transient_error_retry_is_guarded_each_time():
    sink = InMemorySink()
    nebius = FakeAdapter(err("unavailable"), ok(GOOD))
    router, _, sleeps = make_test_router({"nebius": nebius}, sink=sink, daily_budget_usd=D("5"))
    res = await router.generate(req())
    assert res.retries == 1 and sleeps == [1.0] and router._reserved == 0
