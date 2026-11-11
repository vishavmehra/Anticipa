"""Typed view of config/models.toml: providers, per-model pricing and task routes."""

from __future__ import annotations

import tomllib
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .settings import CONFIG_DIR
from .types import Usage

Provider = Literal["anthropic", "nebius"]  # ADR-021: the only two permitted providers
_MTOK = Decimal(1_000_000)
_USD_6DP = Decimal("0.000001")


class RouterSection(BaseModel):
    catalogue_snapshot: str = "provider_catalogue.json"
    repair_attempts: int = Field(1, ge=0, le=3)
    backoff_base_s: float = 1.0
    backoff_max_s: float = 30.0
    retry_after_cap_s: float = 60.0


class ProviderConfig(BaseModel):
    base_url: str | None = None
    timeout_s: float = 120.0
    max_retries: int = Field(2, ge=0, le=6)
    max_concurrency: int = Field(8, ge=1)


class ModelConfig(BaseModel):
    provider: Provider
    id: str
    kind: Literal["chat", "embedding"]
    structured_output: Literal["json_schema", "json_object", "none"] = "json_object"  # Nebius chat only
    native_dimensions: int | None = None
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal | None = None
    cache_write_per_mtok: Decimal | None = None
    verified_on: str

    def cost(self, usage: Usage) -> Decimal:
        """USD for one call. Missing cache prices fall back to the plain input price (never under-bill)."""
        read = self.cache_read_per_mtok if self.cache_read_per_mtok is not None else self.input_per_mtok
        write = self.cache_write_per_mtok if self.cache_write_per_mtok is not None else self.input_per_mtok
        total = (
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_read_tokens * read
            + usage.cache_write_tokens * write
        ) / _MTOK
        return total.quantize(_USD_6DP, rounding=ROUND_HALF_UP)

    def worst_case_cost(self, input_tokens: int, max_output_tokens: int) -> Decimal:
        """Spend-guard upper bound: all input at the dearest input rate (cache write) + full output."""
        in_rate = max(self.input_per_mtok, self.cache_write_per_mtok or Decimal(0))
        return (input_tokens * in_rate + max_output_tokens * self.output_per_mtok) / _MTOK


class Candidate(BaseModel):
    provider: Provider
    model_id: str

    @property
    def ref(self) -> str:
        return f"{self.provider}:{self.model_id}"


class TaskConfig(BaseModel):
    kind: Literal["chat", "embedding"]
    candidates: list[Candidate] = Field(min_length=1)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None  # Claude only
    temperature: float | None = None  # Nebius only
    dimensions: int | None = None  # embedding tasks
    batch_size: int = Field(64, ge=1)

    @field_validator("candidates", mode="before")
    @classmethod
    def _parse_candidates(cls, v: object) -> object:
        out = []
        for item in v if isinstance(v, list) else []:
            if isinstance(item, str):
                provider, sep, model_id = item.partition(":")
                if not sep or not model_id:
                    raise ValueError(f"candidate {item!r} must look like 'provider:model_id'")
                item = {"provider": provider, "model_id": model_id}
            out.append(item)
        return out


class RouterConfig(BaseModel):
    router: RouterSection = RouterSection()
    providers: dict[str, ProviderConfig] = {}
    models: list[ModelConfig] = []
    tasks: dict[str, TaskConfig] = {}
    source_dir: Path = CONFIG_DIR

    def model(self, provider: str, model_id: str) -> ModelConfig:
        for m in self.models:
            if m.provider == provider and m.id == model_id:
                return m
        raise KeyError(f"{provider}:{model_id}")

    def provider(self, name: str) -> ProviderConfig:
        return self.providers.get(name) or ProviderConfig()


def load_config(path: Path | None = None) -> RouterConfig:
    path = path or CONFIG_DIR / "models.toml"
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return RouterConfig.model_validate({**raw, "source_dir": path.parent})
