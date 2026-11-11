"""ClaudeAdapter against a mocked HTTP transport (anthropic 2.6 runs on httpx2, so respx cannot see it)."""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from anticipa.ai.claude import ClaudeAdapter
from anticipa.ai.config import ProviderConfig, load_config
from anticipa.ai.types import AdapterCall, RouterError

pytestmark = pytest.mark.anyio


class Out(BaseModel):
    name: str
    n: int | None


def message(text: str = '{"name": "x", "n": 1}', *, stop: str = "end_turn", content=None, **usage) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": content if content is not None else [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 40,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 300,
            **usage,
        },
    }


def api_error(kind: str, text: str = "boom") -> dict:
    return {"type": "error", "error": {"type": kind, "message": text}}


def adapter_for(handler) -> tuple[ClaudeAdapter, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def wrapped(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(wrapped))
    return ClaudeAdapter("sk-ant-test-not-real", ProviderConfig(timeout_s=5), http_client=client), seen


def call(**kw) -> AdapterCall:
    cfg = load_config()
    base = dict(
        model=cfg.model("anthropic", "claude-haiku-5-5"),
        system="STATIC SYSTEM PROMPT",
        messages=[{"role": "user", "content": "doc"}],
        schema=Out.model_json_schema(),
        max_output_tokens=500,
        effort="low",
    )
    base.update(kw)
    return AdapterCall(**base)


async def test_success_request_shape_and_usage():
    adapter, seen = adapter_for(lambda r: httpx2.Response(200, json=message()))
    resp = await adapter.generate(call())
    assert json.loads(resp.text) == {"name": "x", "n": 1}
    u = resp.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (120, 40, 900, 300)

    body = json.loads(seen[0].content)
    assert seen[0].headers["authorization"] == "Bearer sk-ant-test-not-real"  # SDK 2.6 api_key auth
    assert body["model"] == "claude-haiku-5-5" and body["messages"] == [{"role": "user", "content": "doc"}]
    assert body["system"] == [
        {"type": "text", "text": "STATIC SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["max_tokens"] == 500 + 1024  # visible output + thinking headroom for effort=low
    assert body["output_config"]["effort"] == "low"
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
    assert not {"temperature", "top_p", "top_k", "tool_choice", "thinking"} & body.keys()


async def test_no_schema_no_system_sends_plain_request():
    adapter, seen = adapter_for(lambda r: httpx2.Response(200, json=message("hello")))
    resp = await adapter.generate(call(schema=None, system="", effort=None))
    body = json.loads(seen[0].content)
    assert resp.text == "hello" and "system" not in body and "output_config" not in body


async def test_429_is_rate_limited_with_retry_after():
    adapter, seen = adapter_for(
        lambda r: httpx2.Response(429, headers={"retry-after": "7"}, json=api_error("rate_limit_error"))
    )
    with pytest.raises(RouterError) as ei:
        await adapter.generate(call())
    e = ei.value
    assert (e.category, e.retry_after, e.retryable, e.provider) == ("rate_limited", 7.0, True, "anthropic")
    assert len(seen) == 1  # SDK retries are off: the router owns retrying


@pytest.mark.parametrize(
    ("status", "kind", "category", "retryable"),
    [
        (401, "authentication_error", "auth", False),
        (403, "permission_error", "auth", False),
        (404, "not_found_error", "model_unavailable", False),
        (400, "invalid_request_error", "invalid_request", False),
        (413, "request_too_large", "context_length", False),
        (500, "api_error", "unavailable", True),
        (529, "overloaded_error", "unavailable", True),
    ],
)
async def test_status_mapping(status, kind, category, retryable):
    adapter, _ = adapter_for(lambda r: httpx2.Response(status, json=api_error(kind)))
    with pytest.raises(RouterError) as ei:
        await adapter.generate(call())
    assert (ei.value.category, ei.value.retryable, ei.value.status_code) == (category, retryable, status)


async def test_context_length_400():
    adapter, _ = adapter_for(
        lambda r: httpx2.Response(
            400, json=api_error("invalid_request_error", "prompt is too long: 1200000 tokens")
        )
    )
    with pytest.raises(RouterError) as ei:
        await adapter.generate(call())
    assert ei.value.category == "context_length"


async def test_timeout_and_connection_errors():
    def slow(request):
        raise httpx2.ReadTimeout("slow", request=request)

    def down(request):
        raise httpx2.ConnectError("refused", request=request)

    a1, _ = adapter_for(slow)
    with pytest.raises(RouterError) as ei:
        await a1.generate(call())
    assert ei.value.category == "timeout" and ei.value.retryable
    a2, _ = adapter_for(down)
    with pytest.raises(RouterError) as ei:
        await a2.generate(call())
    assert ei.value.category == "unavailable" and ei.value.retryable


async def test_refusal_is_checked_before_content():
    refused = message(content=[], stop="refusal")
    refused["stop_details"] = {"type": "refusal", "category": "cyber", "explanation": "declined"}
    adapter, _ = adapter_for(lambda r: httpx2.Response(200, json=refused))
    with pytest.raises(RouterError) as ei:
        await adapter.generate(call())
    assert ei.value.category == "refusal" and not ei.value.retryable and "cyber" in ei.value.message


async def test_truncated_and_empty_outputs_are_validation_failures():
    a1, _ = adapter_for(lambda r: httpx2.Response(200, json=message('{"name": "x', stop="max_tokens")))
    with pytest.raises(RouterError, match="truncated") as ei:
        await a1.generate(call())
    assert ei.value.category == "output_validation"
    a2, _ = adapter_for(
        lambda r: httpx2.Response(
            200, json=message(content=[{"type": "thinking", "thinking": "", "signature": "s"}])
        )
    )
    with pytest.raises(RouterError, match="empty"):
        await a2.generate(call())


async def test_list_model_ids_and_auth_failure():
    page = {
        "data": [
            {
                "id": "claude-haiku-5-5",
                "type": "model",
                "display_name": "H",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ],
        "has_more": False,
        "first_id": "claude-haiku-5-5",
        "last_id": "claude-haiku-5-5",
    }
    adapter, seen = adapter_for(lambda r: httpx2.Response(200, json=page))
    assert await adapter.list_model_ids() == ["claude-haiku-5-5"]
    assert seen[0].url.path.endswith("/v1/models")
    bad, _ = adapter_for(lambda r: httpx2.Response(401, json=api_error("authentication_error")))
    with pytest.raises(RouterError) as ei:
        await bad.list_model_ids()
    assert ei.value.category == "auth"
