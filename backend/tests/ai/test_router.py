"""Router behaviour: retries, fallback, validation/repair, refusal, cost math, cache, embeddings, sinks."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import BaseModel

from anticipa.ai.cache import ResponseCache
from anticipa.ai.testing import FakeAdapter, err, ok
from anticipa.ai.types import EmbedRequest, GenerateRequest, RouterError, Usage

pytestmark = pytest.mark.anyio


class Fact(BaseModel):
    name: str
    n: int


GOOD = json.dumps({"name": "x", "n": 1})


def req(**kw) -> GenerateRequest:
    base = dict(
        task="evidence.extract",
        system="sys",
        messages=[{"role": "user", "content": "doc text SECRET-1"}],
        output_schema=Fact,
        prompt_version="t@1",
        metadata={"job_id": "not-a-uuid"},
    )
    base.update(kw)
    return GenerateRequest(**base)


async def test_success_primary_cost_and_record(make_router):
    nebius = FakeAdapter(ok(GOOD, i=1000, o=500))
    router, sink, _ = make_router({"nebius": nebius})
    res = await router.generate(req())
    assert res.output == Fact(name="x", n=1)
    assert (res.provider, res.model_id) == ("nebius", "Qwen/Qwen3-235B-A22B-Instruct-2507")
    assert (
        not res.fallback_used and res.retries == 0 and res.repairs == 0 and res.validation_status == "valid"
    )
    # 1000 in * $0.20/M + 500 out * $0.60/M = 0.0002 + 0.0003
    assert res.cost_usd == Decimal("0.000500")
    [rec] = sink.records
    assert rec.id == res.invocation_id and rec.status == "succeeded" and rec.cost_usd == Decimal("0.000500")
    assert rec.input_tokens == 1000 and rec.fallback_used is False


async def test_cost_math_includes_cache_tokens(cfg):
    m = cfg.model("anthropic", "claude-opus-5-5")
    u = Usage(
        input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=2_000_000, cache_write_tokens=200_000
    )
    # 4.00 + 2.00 + 2*0.20 + 0.2*5.00
    assert m.cost(u) == Decimal("7.400000")
    assert cfg.model("nebius", "Qwen/Qwen3-Embedding-8B").cost(Usage(input_tokens=86)) == Decimal("0.000001")


async def test_retry_honours_retry_after_then_succeeds(make_router):
    nebius = FakeAdapter(err("rate_limited", retry_after=5.0), err("unavailable"), ok(GOOD))
    router, sink, sleeps = make_router({"nebius": nebius})
    res = await router.generate(req())
    assert res.retries == 2 and not res.fallback_used
    assert sleeps[0] == 5.0  # retry-after beats the 1s backoff
    assert sleeps[1] == 2.0  # base 1.0 * 2^1, jitter factor 1.0
    assert len(sink.records) == 1


async def test_retry_after_is_capped(make_router, cfg):
    nebius = FakeAdapter(err("rate_limited", retry_after=3600.0), ok(GOOD))
    router, _, sleeps = make_router({"nebius": nebius})
    await router.generate(req())
    assert sleeps == [cfg.router.retry_after_cap_s]


async def test_retries_exhausted_falls_back_and_accounts_both(make_router):
    nebius = FakeAdapter(*[err("unavailable")] * 3)  # 1 try + 2 retries
    claude = FakeAdapter(ok(GOOD, i=100, o=50))
    router, sink, _ = make_router({"nebius": nebius, "anthropic": claude})
    res = await router.generate(req())
    assert res.fallback_used and res.provider == "anthropic" and res.model_id == "claude-haiku-5-5"
    failed, good = sink.records
    assert (failed.status, failed.error_category, failed.fallback_used, failed.retry_count) == (
        "failed",
        "unavailable",
        False,
        2,
    )
    assert (good.status, good.fallback_used) == ("succeeded", True)
    # Claude gets effort, not temperature; Nebius gets temperature, not effort
    assert claude.calls[0].effort == "low" and claude.calls[0].temperature is None
    assert nebius.calls[0].temperature == 0.0 and nebius.calls[0].effort is None


@pytest.mark.parametrize("category", ["invalid_request", "model_unavailable", "context_length", "unknown"])
async def test_non_retryable_not_retried_but_falls_back(make_router, category):
    nebius = FakeAdapter(err(category))
    claude = FakeAdapter(ok(GOOD))
    router, _, sleeps = make_router({"nebius": nebius, "anthropic": claude})
    res = await router.generate(req())
    assert res.fallback_used and len(nebius.calls) == 1 and sleeps == []


async def test_malformed_json_is_repaired_without_echoing_input(make_router):
    nebius = FakeAdapter(ok('{"name": "x", "n": "SECRET-LEAK"}', i=100, o=20), ok(GOOD, i=150, o=30))
    router, sink, _ = make_router({"nebius": nebius})
    res = await router.generate(req())
    assert res.repairs == 1 and res.validation_status == "repaired" and not res.fallback_used
    assert res.usage.input_tokens == 250 and res.usage.output_tokens == 50  # both turns are billed
    repair_msgs = nebius.calls[1].messages
    assert [m["role"] for m in repair_msgs] == ["user", "assistant", "user"]
    assert "n: " in repair_msgs[-1]["content"] and "SECRET-LEAK" not in repair_msgs[-1]["content"]
    assert sink.records[0].validation_status == "repaired"


async def test_repair_fails_then_fallback_validates_again(make_router):
    nebius = FakeAdapter(ok("not json"), ok("{still: bad"))
    claude = FakeAdapter(ok(GOOD))
    router, sink, _ = make_router({"nebius": nebius, "anthropic": claude})
    res = await router.generate(req())
    assert res.fallback_used and res.provider == "anthropic" and res.output.n == 1
    failed, good = sink.records
    assert failed.error_category == "output_validation" and failed.validation_status == "failed"
    assert failed.input_tokens == 200  # cost of the two wasted turns is not lost
    assert good.validation_status == "valid"


async def test_fallback_output_must_also_validate(make_router):
    nebius = FakeAdapter(ok("nope"), ok("nope"))
    claude = FakeAdapter(ok('{"name": "x"}'), ok('{"name": "x"}'))  # missing n -> still invalid after repair
    router, sink, _ = make_router({"nebius": nebius, "anthropic": claude})
    with pytest.raises(RouterError) as ei:
        await router.generate(req())
    assert ei.value.category == "output_validation"
    assert [r.status for r in sink.records] == ["failed", "failed"]


async def test_fenced_json_is_accepted(make_router):
    router, _, _ = make_router({"nebius": FakeAdapter(ok("```json\n" + GOOD + "\n```"))})
    assert (await router.generate(req())).output.n == 1


async def test_refusal_never_falls_back(make_router):
    nebius = FakeAdapter(err("refusal", provider="nebius"))
    claude = FakeAdapter(ok(GOOD))
    router, sink, _ = make_router({"nebius": nebius, "anthropic": claude})
    with pytest.raises(RouterError) as ei:
        await router.generate(req())
    assert ei.value.category == "refusal" and claude.calls == []
    assert sink.records[0].error_category == "refusal"


async def test_auth_failure_marks_provider_dead_but_other_provider_serves(make_router):
    router, _, _ = make_router({"anthropic": FakeAdapter(ok(GOOD)), "nebius": FakeAdapter(err("auth"))})
    res = await router.generate(req())
    assert res.provider == "anthropic" and res.fallback_used


async def test_missing_provider_key_is_skipped_not_fatal(make_router):
    router, _, _ = make_router({"anthropic": FakeAdapter(ok(GOOD))})  # no nebius adapter (no key)
    assert (await router.generate(req())).provider == "anthropic"


async def test_all_candidates_failing_raises_last_error(make_router):
    router, _, _ = make_router({})
    with pytest.raises(RouterError) as ei:
        await router.generate(req())
    assert ei.value.category == "auth"


async def test_request_validation(make_router):
    router, _, _ = make_router({"nebius": FakeAdapter()})
    with pytest.raises(RouterError, match="unknown task"):
        await router.generate(req(task="nope"))
    with pytest.raises(RouterError, match="embedding task"):
        await router.generate(req(task="embedding.document"))
    with pytest.raises(RouterError, match="prefill"):
        await router.generate(
            req(messages=[{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
        )


async def test_text_output_without_schema(make_router):
    router, _, _ = make_router({"anthropic": FakeAdapter(ok("hello"))})
    res = await router.generate(req(task="engagement.draft", output_schema=None))
    assert res.output == "hello" and res.validation_status == "not_applicable" and res.provider == "anthropic"


async def test_sink_failure_does_not_lose_result(make_router):
    class Boom:
        async def record(self, rec):
            raise RuntimeError("db down")

    router, _, _ = make_router({"nebius": FakeAdapter(ok(GOOD))}, sink=Boom())
    assert (await router.generate(req())).output.n == 1


# ---------------------------------------------------------------------------------- cache
async def test_cache_replays_without_calling_provider(make_router, tmp_path):
    nebius = FakeAdapter(ok(GOOD, i=1000, o=500))
    router, sink, _ = make_router({"nebius": nebius}, cache=ResponseCache(tmp_path))
    first = await router.generate(req())
    second = await router.generate(req())
    assert len(nebius.calls) == 1
    assert (
        not first.cached
        and second.cached
        and second.cost_usd == Decimal("0")
        and second.output == first.output
    )
    assert [(r.status, r.cache_hit) for r in sink.records] == [("succeeded", False), ("succeeded", True)]
    assert sink.total_cost_usd == first.cost_usd


async def test_unwritable_cache_never_fails_a_paid_call(make_router, tmp_path):
    """A non-root container user cannot create the cache folder: the call still succeeds, uncached."""
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")  # mkdir under a file raises OSError, like a read-only /app/data
    nebius = FakeAdapter(ok(GOOD), ok(GOOD))
    router, _, _ = make_router({"nebius": nebius}, cache=ResponseCache(blocker / "cache"))
    first, second = await router.generate(req()), await router.generate(req())
    assert first.output == second.output and not second.cached and len(nebius.calls) == 2


async def test_cache_key_covers_prompt_version_messages_and_schema(make_router, tmp_path):
    nebius = FakeAdapter(ok(GOOD), ok(GOOD), ok(GOOD))
    router, _, _ = make_router({"nebius": nebius}, cache=ResponseCache(tmp_path))
    await router.generate(req())
    await router.generate(req(prompt_version="t@2"))
    await router.generate(req(messages=[{"role": "user", "content": "other"}]))
    assert len(nebius.calls) == 3


async def test_cache_entry_is_revalidated_and_corruption_is_a_miss(make_router, tmp_path):
    cache = ResponseCache(tmp_path)
    nebius = FakeAdapter(ok(GOOD), ok(GOOD))
    router, _, _ = make_router({"nebius": nebius}, cache=cache)
    await router.generate(req())
    [path] = list(tmp_path.rglob("*.json"))
    path.write_text(json.dumps({"text": "not json", "provider": "nebius", "model_id": "m"}), encoding="utf-8")
    res = await router.generate(req())  # invalid cached text -> miss -> fresh call
    assert not res.cached and len(nebius.calls) == 2
    path.write_text("{garbage", encoding="utf-8")
    assert ResponseCache(tmp_path).get("whatever") is None


# ---------------------------------------------------------------------------------- embeddings
async def test_embed_batches_and_reports_dimensions(make_router, cfg):
    cfg.tasks["embedding.document"].batch_size = 2
    nebius = FakeAdapter(ok("", i=10, o=0), ok("", i=10, o=0), ok("", i=10, o=0))
    router, sink, _ = make_router({"nebius": nebius})
    res = await router.embed(EmbedRequest(texts=["a", "b", "c", "d", "e"]))
    assert nebius.embed_calls == [["a", "b"], ["c", "d"], ["e"]]
    assert len(res.vectors) == 5 and res.dimensions == 1024 and len(res.vectors[0]) == 1024
    assert (
        res.usage.input_tokens == 30
        and len(sink.records) == 1
        and sink.records[0].task == "embedding.document"
    )


async def test_embed_rejects_blank_and_wrong_kind(make_router):
    router, _, _ = make_router({"nebius": FakeAdapter()})
    with pytest.raises(RouterError, match="non-blank"):
        await router.embed(EmbedRequest(texts=["ok", "  "]))
    with pytest.raises(RouterError, match="chat task"):
        await router.embed(EmbedRequest(task="evidence.extract", texts=["x"]))


async def test_embed_provider_error_is_recorded(make_router):
    router, sink, _ = make_router({"nebius": FakeAdapter(err("invalid_request"))})
    with pytest.raises(RouterError):
        await router.embed(EmbedRequest(texts=["x"]))
    assert sink.records[0].status == "failed" and sink.records[0].error_category == "invalid_request"
