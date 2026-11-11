"""Nebius Token Factory adapter (OpenAI-compatible HTTP; the only module that talks to Nebius).

Bearer auth, `chat/completions` and `embeddings`. Structured output mode is per model in models.toml:
`json_schema` (response_format json_schema), `json_object` (JSON mode + schema in the prompt) or `none`.
The router validates every response against the pydantic schema either way, and does the repair turn.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .config import ModelConfig, ProviderConfig
from .types import AdapterCall, AdapterResponse, RouterError, Usage

_CONTEXT_RE = re.compile(r"context.{0,20}(length|window)|too long|maximum.{0,20}tokens", re.I)
_MODEL_RE = re.compile(r"model.{0,40}(not found|does not exist|unknown|not supported)", re.I)


def _error_message(r: httpx.Response) -> str:
    try:
        body = r.json()
        err = body.get("error", body) if isinstance(body, dict) else body
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return str(msg or r.text)[:300]
    except ValueError:
        return r.text[:300]


def map_http_error(r: httpx.Response, model_id: str) -> RouterError:
    code, msg = r.status_code, _error_message(r)
    kw: dict[str, Any] = {"provider": "nebius", "model_id": model_id, "status_code": code}
    try:
        retry_after = float(r.headers.get("retry-after", ""))
    except ValueError:
        retry_after = None
    if code in (401, 403):
        return RouterError("auth", msg or "authentication failed", **kw)
    if code == 404 or (code in (400, 422) and _MODEL_RE.search(msg)):
        return RouterError("model_unavailable", msg or "model not found", **kw)
    if code == 413 or (code == 400 and _CONTEXT_RE.search(msg)):
        return RouterError("context_length", msg, **kw)
    if code in (400, 422):
        return RouterError("invalid_request", msg, **kw)
    if code == 408:
        return RouterError("timeout", msg or "request timeout", **kw)
    if code == 429:
        return RouterError("rate_limited", msg or "rate limited", retry_after=retry_after, **kw)
    if code >= 500:
        return RouterError("unavailable", msg or f"server error {code}", retry_after=retry_after, **kw)
    return RouterError("unknown", msg, **kw)


def _schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "Respond with a single JSON object and nothing else. It must validate against this JSON Schema:\n"
        + json.dumps(schema, ensure_ascii=False)
    )


class NebiusAdapter:
    provider = "nebius"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        cfg: ProviderConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=cfg.timeout_s,
            transport=transport,
        )

    async def _post(self, path: str, body: dict[str, Any], model_id: str) -> dict[str, Any]:
        try:
            r = await self._http.post(path, json=body)
        except httpx.TimeoutException as e:
            raise RouterError("timeout", "request timed out", provider="nebius", model_id=model_id) from e
        except httpx.TransportError as e:
            raise RouterError(
                "unavailable", f"connection error ({type(e).__name__})", provider="nebius", model_id=model_id
            ) from e
        except httpx.HTTPError as e:
            raise RouterError(
                "unknown", f"http error ({type(e).__name__})", provider="nebius", model_id=model_id
            ) from e
        if r.status_code >= 400:
            raise map_http_error(r, model_id)
        try:
            data = r.json()
        except ValueError as e:
            raise RouterError(
                "output_validation", "provider returned non-JSON body", provider="nebius", model_id=model_id
            ) from e
        if not isinstance(data, dict):
            raise RouterError(
                "output_validation",
                "unexpected provider response shape",
                provider="nebius",
                model_id=model_id,
            )
        return data

    @staticmethod
    def _usage(data: dict[str, Any]) -> Usage:
        u = data.get("usage") or {}
        prompt = int(u.get("prompt_tokens") or 0)
        cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        return Usage(
            input_tokens=max(prompt - cached, 0),
            output_tokens=int(u.get("completion_tokens") or 0),
            cache_read_tokens=cached,
        )

    async def generate(self, call: AdapterCall) -> AdapterResponse:
        model: ModelConfig = call.model
        system = call.system
        body: dict[str, Any] = {"model": model.id, "max_tokens": call.max_output_tokens}
        if call.temperature is not None:
            body["temperature"] = call.temperature
        if call.schema:
            if model.structured_output == "json_schema":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "response", "schema": call.schema, "strict": True},
                }
            else:
                if model.structured_output == "json_object":
                    body["response_format"] = {"type": "json_object"}
                system = f"{system}\n\n{_schema_instruction(call.schema)}".strip()
        body["messages"] = ([{"role": "system", "content": system}] if system else []) + call.messages

        data = await self._post("chat/completions", body, model.id)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise RouterError(
                "output_validation",
                "provider response has no choices[0].message.content",
                provider="nebius",
                model_id=model.id,
            ) from e
        if choice.get("finish_reason") == "length":
            raise RouterError(
                "output_validation", "output truncated at max_tokens", provider="nebius", model_id=model.id
            )
        if not text.strip():
            raise RouterError("output_validation", "empty response", provider="nebius", model_id=model.id)
        return AdapterResponse(text=text, usage=self._usage(data), provider_request_id=data.get("id"))

    async def embed(
        self, model: ModelConfig, texts: list[str], dimensions: int
    ) -> tuple[list[list[float]], Usage]:
        data = await self._post(
            "embeddings", {"model": model.id, "input": texts, "dimensions": dimensions}, model.id
        )
        try:
            items = sorted(data["data"], key=lambda d: d["index"])
            vectors = [[float(x) for x in d["embedding"]] for d in items]
        except (KeyError, TypeError, ValueError) as e:
            raise RouterError(
                "output_validation", "malformed embeddings response", provider="nebius", model_id=model.id
            ) from e
        if len(vectors) != len(texts) or any(len(v) != dimensions for v in vectors):
            raise RouterError(
                "output_validation",
                f"expected {len(texts)} vectors of {dimensions} dims, got {len(vectors)} of "
                f"{sorted({len(v) for v in vectors})}",
                provider="nebius",
                model_id=model.id,
            )
        return vectors, self._usage(data)

    async def list_model_ids(self) -> list[str]:
        try:
            r = await self._http.get("models")
        except httpx.TimeoutException as e:
            raise RouterError("timeout", "request timed out", provider="nebius") from e
        except httpx.HTTPError as e:
            raise RouterError(
                "unavailable", f"connection error ({type(e).__name__})", provider="nebius"
            ) from e
        if r.status_code >= 400:
            raise map_http_error(r, "(models list)")
        return [m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and "id" in m]

    async def aclose(self) -> None:
        await self._http.aclose()
