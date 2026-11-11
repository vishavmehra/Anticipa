"""Typed request/response objects and the normalised error taxonomy for the Model Router."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ErrorCategory = Literal[
    "auth",
    "rate_limited",
    "timeout",
    "unavailable",
    "invalid_request",
    "model_unavailable",
    "context_length",
    "output_validation",
    "refusal",
    "budget_exceeded",
    "unknown",
]
# Transport-level categories worth retrying on the same candidate.
RETRYABLE: frozenset[str] = frozenset({"rate_limited", "timeout", "unavailable"})


class RouterError(Exception):
    """Provider failure normalised to one of the contract categories. Never carries secrets."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        provider: str | None = None,
        model_id: str | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(f"[{category}] {message}")
        self.category: ErrorCategory = category
        self.message = message
        self.provider = provider
        self.model_id = model_id
        self.status_code = status_code
        self.retry_after = retry_after
        self.retryable = (category in RETRYABLE) if retryable is None else retryable
        self.retries_used = 0  # transport retries spent before this error surfaced (set by the router)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class Usage(BaseModel):
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0  # includes Claude thinking tokens (billed as output)
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass
class AdapterCall:
    """What the router hands a provider adapter for one attempt (provider-neutral)."""

    model: Any  # ModelConfig (typed Any to avoid an import cycle with config.py)
    system: str
    messages: list[dict[str, str]]
    schema: dict[str, Any] | None
    max_output_tokens: int
    effort: str | None = None  # Claude only
    temperature: float | None = None  # Nebius only


@dataclass
class AdapterResponse:
    text: str
    usage: Usage
    provider_request_id: str | None = None


class GenerateRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: str  # key in config/models.toml [tasks.*]
    system: str = ""  # static instructions; Claude prompt-caches this block
    messages: list[ChatMessage]  # content is untrusted data (ADR-039)
    output_schema: type[BaseModel] | None = None  # validated before return, on every candidate
    prompt_version: str
    max_output_tokens: int = 4000
    metadata: dict[str, Any] = Field(default_factory=dict)  # document_id / job_id; never sent to a provider


class GenerateResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    output: Any  # output_schema instance when a schema was given, else the text
    text: str
    provider: str
    model_id: str
    task: str
    prompt_version: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    retries: int  # transport retries on the serving candidate
    repairs: int  # validation repair turns on the serving candidate
    fallback_used: bool
    invocation_id: str
    request_id: str
    cached: bool = False
    validation_status: Literal["valid", "repaired", "not_applicable"] = "not_applicable"


class EmbedRequest(BaseModel):
    task: str = "embedding.document"
    texts: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmbedResult(BaseModel):
    vectors: list[list[float]]
    dimensions: int
    provider: str
    model_id: str
    task: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    retries: int
    fallback_used: bool
    invocation_id: str
    request_id: str
