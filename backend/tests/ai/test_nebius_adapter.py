"""NebiusAdapter against respx-mocked OpenAI-compatible endpoints."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import BaseModel

from anticipa.ai.config import ProviderConfig, load_config
from anticipa.ai.nebius import NebiusAdapter
from anticipa.ai.types import AdapterCall, RouterError

pytestmark = pytest.mark.anyio
BASE = "https://nebius.test/v1/"


class Out(BaseModel):
    name: str


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def adapter():
    return NebiusAdapter("test-key-not-real", BASE, ProviderConfig(timeout_s=5))


def chat_body(text='{"name": "x"}', finish="stop", usage=None):
    return {
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": text}}],
        "usage": usage or {"prompt_tokens": 80, "completion_tokens": 12},
    }


def call(cfg, mode="json_schema", **kw):
    model = cfg.model("nebius", "Qwen/Qwen3-235B-A22B-Instruct-2507").model_copy(
        update={"structured_output": mode}
    )
    base = dict(
        model=model,
        system="SYS",
        messages=[{"role": "user", "content": "doc"}],
        schema=Out.model_json_schema(),
        max_output_tokens=300,
        temperature=0.0,
    )
    base.update(kw)
    return AdapterCall(**base)


@respx.mock
async def test_json_schema_mode_request_and_usage(adapter, cfg):
    route = respx.post(BASE + "chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=chat_body(
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 40},
                }
            ),
        )
    )
    resp = await adapter.generate(call(cfg))
    sent = route.calls[0].request
    body = json.loads(sent.content)
    assert sent.headers["authorization"] == "Bearer test-key-not-real"
    assert (
        body["response_format"]["type"] == "json_schema"
        and body["response_format"]["json_schema"]["strict"] is True
    )
    assert body["response_format"]["json_schema"]["schema"]["properties"]["name"]["type"] == "string"
    assert body["messages"][0] == {"role": "system", "content": "SYS"} and body["temperature"] == 0.0
    assert body["model"] == "Qwen/Qwen3-235B-A22B-Instruct-2507" and body["max_tokens"] == 300
    assert (resp.usage.input_tokens, resp.usage.cache_read_tokens, resp.usage.output_tokens) == (60, 40, 10)
    assert resp.provider_request_id == "chatcmpl-1"


@respx.mock
async def test_json_object_mode_puts_schema_in_prompt(adapter, cfg):
    route = respx.post(BASE + "chat/completions").mock(return_value=httpx.Response(200, json=chat_body()))
    await adapter.generate(call(cfg, mode="json_object"))
    body = json.loads(route.calls[0].request.content)
    assert body["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in body["messages"][0]["content"] and '"name"' in body["messages"][0]["content"]


@respx.mock
async def test_429_retry_after_header(adapter, cfg):
    respx.post(BASE + "chat/completions").mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "3"}, json={"error": {"message": "slow down"}}
        )
    )
    with pytest.raises(RouterError) as ei:
        await adapter.generate(call(cfg))
    assert (ei.value.category, ei.value.retry_after, ei.value.retryable) == ("rate_limited", 3.0, True)


@respx.mock
@pytest.mark.parametrize(
    ("status", "payload", "category"),
    [
        (401, {"error": {"message": "invalid api key"}}, "auth"),
        (403, {"error": {"message": "forbidden"}}, "auth"),
        (404, {"error": {"message": "no such route"}}, "model_unavailable"),
        (400, {"error": {"message": "The model `x` does not exist"}}, "model_unavailable"),
        (400, {"error": {"message": "maximum context length is 262144 tokens"}}, "context_length"),
        (400, {"error": {"message": "bad schema"}}, "invalid_request"),
        (503, {"error": {"message": "overloaded"}}, "unavailable"),
    ],
)
async def test_status_mapping(adapter, cfg, status, payload, category):
    respx.post(BASE + "chat/completions").mock(return_value=httpx.Response(status, json=payload))
    with pytest.raises(RouterError) as ei:
        await adapter.generate(call(cfg))
    assert ei.value.category == category and ei.value.status_code == status


@respx.mock
async def test_timeout_and_connect_error(adapter, cfg):
    respx.post(BASE + "chat/completions").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(RouterError) as ei:
        await adapter.generate(call(cfg))
    assert ei.value.category == "timeout" and ei.value.retryable
    respx.post(BASE + "chat/completions").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(RouterError) as ei:
        await adapter.generate(call(cfg))
    assert ei.value.category == "unavailable"


@respx.mock
async def test_bad_responses_are_output_validation_errors(adapter, cfg):
    cases = [
        httpx.Response(200, json=chat_body(finish="length")),
        httpx.Response(200, json=chat_body(text="   ")),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, content=b"<html>gateway</html>"),
    ]
    for response in cases:
        respx.post(BASE + "chat/completions").mock(return_value=response)
        with pytest.raises(RouterError) as ei:
            await adapter.generate(call(cfg))
        assert ei.value.category == "output_validation"


@respx.mock
async def test_embeddings_send_dimensions_and_sort_by_index(adapter, cfg):
    route = respx.post(BASE + "embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                ],
                "usage": {"prompt_tokens": 12, "total_tokens": 12},
            },
        )
    )
    model = cfg.model("nebius", "Qwen/Qwen3-Embedding-8B")
    vectors, usage = await adapter.embed(model, ["first", "second"], 3)
    body = json.loads(route.calls[0].request.content)
    assert body == {"model": "Qwen/Qwen3-Embedding-8B", "input": ["first", "second"], "dimensions": 3}
    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]] and usage.input_tokens == 12


@respx.mock
async def test_embeddings_wrong_dimension_or_count_is_rejected(adapter, cfg):
    model = cfg.model("nebius", "Qwen/Qwen3-Embedding-8B")
    respx.post(BASE + "embeddings").mock(
        return_value=httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}], "usage": {"prompt_tokens": 1}}
        )
    )
    with pytest.raises(RouterError, match="expected 1 vectors of 3 dims") as ei:
        await adapter.embed(model, ["a"], 3)
    assert ei.value.category == "output_validation"
    with pytest.raises(RouterError, match="expected 2 vectors"):
        await adapter.embed(model, ["a", "b"], 2)


@respx.mock
async def test_list_models(adapter):
    respx.get(BASE + "models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "a/b"}, {"id": "c"}]})
    )
    assert await adapter.list_model_ids() == ["a/b", "c"]
    respx.get(BASE + "models").mock(return_value=httpx.Response(401, json={"error": {"message": "no"}}))
    with pytest.raises(RouterError) as ei:
        await adapter.list_model_ids()
    assert ei.value.category == "auth"
