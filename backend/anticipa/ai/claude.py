"""Anthropic Claude adapter (the only module that imports the `anthropic` SDK).

Facts this adapter depends on (claude-api skill, SDK anthropic 2.6.0, introspected):
- structured output = `output_config.format {"type": "json_schema"}`; forced tool_choice returns 400.
- claude-opus-5-5 / haiku-5-5 think by default (cannot be disabled on opus) -> depth via output_config.effort;
  thinking tokens count against max_tokens, so we add headroom per effort level.
- no temperature/top_p, no assistant prefill. Always check stop_reason == "refusal" before reading content.
- SDK retries are disabled (max_retries=0): the router owns retry/backoff/fallback so it can count them.
"""

from __future__ import annotations

import re
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from .config import ProviderConfig
from .types import AdapterCall, AdapterResponse, RouterError, Usage

_THINKING_HEADROOM = {"low": 1024, "medium": 4096, "high": 8192, "xhigh": 12000, "max": 16000}
_MAX_TOKENS_CAP = 20000  # keeps the request inside the SDK's non-streaming limit
_CONTEXT_RE = re.compile(r"too long|context (window|length)|exceeds? .*(limit|maximum)", re.I)


def _retry_after(e: anthropic.APIStatusError) -> float | None:
    try:
        return float(e.response.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return None


def map_anthropic_error(e: Exception, model_id: str) -> RouterError:
    kw: dict[str, Any] = {"provider": "anthropic", "model_id": model_id}
    if isinstance(e, anthropic.APITimeoutError):  # before APIConnectionError (its parent)
        return RouterError("timeout", "request timed out", **kw)
    if isinstance(e, anthropic.APIConnectionError):
        return RouterError("unavailable", "connection error", **kw)
    if not isinstance(e, anthropic.APIStatusError):
        return RouterError("unknown", f"unexpected {type(e).__name__}", **kw)
    code, msg = e.status_code, str(getattr(e, "message", "") or "")[:300]
    kw["status_code"] = code
    if isinstance(e, anthropic.EnforcedSpendLimitReachedError):  # before RateLimitError (its parent)
        return RouterError("rate_limited", f"spend limit reached: {msg}", retryable=False, **kw)
    if isinstance(e, anthropic.OrganizationOnHoldError):  # before BadRequestError (its parent)
        return RouterError("auth", f"organization on hold: {msg}", **kw)
    if code in (401, 403):
        return RouterError("auth", msg or "authentication failed", **kw)
    if code == 404:
        return RouterError("model_unavailable", msg or "model not found or not available", **kw)
    if code == 413 or (code == 400 and _CONTEXT_RE.search(msg)):
        return RouterError("context_length", msg, **kw)
    if code in (400, 422):
        return RouterError("invalid_request", msg, **kw)
    if code == 408:
        return RouterError("timeout", msg or "request timeout", **kw)
    if code == 429:
        return RouterError("rate_limited", msg or "rate limited", retry_after=_retry_after(e), **kw)
    if code >= 500:  # includes 529 overloaded, 503, 504
        return RouterError("unavailable", msg or f"server error {code}", retry_after=_retry_after(e), **kw)
    return RouterError("unknown", msg, **kw)


class ClaudeAdapter:
    provider = "anthropic"

    def __init__(self, api_key: str, cfg: ProviderConfig, *, http_client: Any = None) -> None:
        self._client = AsyncAnthropic(
            api_key=api_key,
            timeout=cfg.timeout_s,
            max_retries=0,
            base_url=cfg.base_url,
            http_client=http_client,
        )

    @staticmethod
    def effective_max_tokens(call: AdapterCall) -> int:
        """Billing ceiling of one call: visible output plus thinking headroom (billed as output)."""
        headroom = _THINKING_HEADROOM.get(call.effort or "medium", 4096)
        return min(call.max_output_tokens + headroom, _MAX_TOKENS_CAP)

    async def generate(self, call: AdapterCall) -> AdapterResponse:
        model_id = call.model.id
        params: dict[str, Any] = {
            "model": model_id,
            "max_tokens": self.effective_max_tokens(call),
            "messages": call.messages,
        }
        if call.system:  # static system prompt first + cache breakpoint: reused across documents
            params["system"] = [{"type": "text", "text": call.system, "cache_control": {"type": "ephemeral"}}]
        output_config: dict[str, Any] = {}
        if call.effort:
            output_config["effort"] = call.effort
        if call.schema:
            output_config["format"] = {
                "type": "json_schema",
                "schema": anthropic.transform_schema(call.schema),
            }
        if output_config:
            params["output_config"] = output_config
        try:
            msg = await self._client.messages.create(**params)
        except Exception as e:  # noqa: BLE001 - everything is normalised to RouterError
            raise map_anthropic_error(e, model_id) from e

        if msg.stop_reason == "refusal":  # check before reading content (skill: refusal handling)
            category = getattr(msg.stop_details, "category", None)
            raise RouterError(
                "refusal",
                f"model declined the request (category={category})",
                provider="anthropic",
                model_id=model_id,
                retryable=False,
            )
        text = "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason == "max_tokens":
            raise RouterError(
                "output_validation", "output truncated at max_tokens", provider="anthropic", model_id=model_id
            )
        if not text.strip():
            raise RouterError("output_validation", "empty response", provider="anthropic", model_id=model_id)
        u = msg.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
        )
        return AdapterResponse(text=text, usage=usage, provider_request_id=getattr(msg, "_request_id", None))

    async def list_model_ids(self) -> list[str]:
        try:
            return [m.id async for m in self._client.models.list(limit=1000)]
        except Exception as e:  # noqa: BLE001
            raise map_anthropic_error(e, "(models list)") from e

    async def aclose(self) -> None:
        await self._client.close()
