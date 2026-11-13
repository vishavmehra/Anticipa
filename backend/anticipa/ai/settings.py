"""Self-contained AI settings: provider keys from the repo-root .env (never printed, never logged)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/anticipa/ai/settings.py -> repo root is three levels above the package directory.
REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"
_PLACEHOLDERS = {"", "replace-me"}


class AiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: SecretStr | None = None
    nebius_api_key: SecretStr | None = None
    nebius_base_url: str | None = None  # None -> models.toml providers.nebius.base_url
    # Public-demo spend guard: a call whose worst-case cost would push today's (UTC) spend past
    # this is refused with RouterError(budget_exceeded).
    anticipa_ai_daily_budget_usd: Decimal = Field(Decimal("5.00"), ge=0)
    # Per-run cap (one job run = one router): the sum of that run's spend, including a resumed run's earlier
    # attempts, may not exceed this. Enforced in addition to the daily cap.
    anticipa_ai_run_budget_usd: Decimal = Field(Decimal("40.00"), ge=0)
    # Hard cap on Sonnet escalations as a share of the cohort's documents (ceiling 10%; the 10k run uses 2%).
    anticipa_ai_escalation_share: Decimal = Field(Decimal("0.10"), ge=0, le=Decimal("0.10"))
    # Owner decision 2026-11-11: Nebius credits are preserved. With this unset no Nebius adapter exists and
    # no route (embeddings and fallbacks included) may use Nebius.
    anticipa_enable_nebius: bool = False
    # Nebius credit ledger (owner: preserving the $50 balance is a very high priority). Lifetime Nebius spend
    # recorded in model_invocations plus in-flight reservations may never exceed this.
    anticipa_nebius_credit_usd: Decimal = Field(Decimal("50.00"), ge=0)
    # Lifetime Anthropic spend cap (owner keeps part of the credit): recorded spend in model_invocations plus
    # in-flight reservations may never exceed this. None = no lifetime cap.
    anticipa_anthropic_spend_cap_usd: Decimal | None = Field(None, ge=0)
    # Nebius runs also need explicit approval: an embedding run refuses to start unless this (or the
    # per-request
    # approve_cost_usd) is >= the estimate computed from the actual selected texts, and it hard-stops at it.
    anticipa_nebius_run_approved_usd: Decimal | None = Field(None, ge=0)
    # Every successful model output is cached on disk and replayed, so a rerun never re-bills.
    # Set ANTICIPA_LLM_CACHE=0 to force fresh calls (evaluation of a changed prompt, for example).
    anticipa_llm_cache: bool = True
    anticipa_llm_cache_dir: Path = REPO_ROOT / "data" / "cache" / "llm"
    # Ask Anticipa (analytics Q and A): its own daily cost cap (task ask.analytics only, UTC day) and per-user
    # request limits, enforced before any model call. The router's daily and lifetime caps still apply on top.
    anticipa_ask_daily_budget_usd: Decimal = Field(Decimal("2.00"), ge=0)
    anticipa_ask_rate_per_hour: int = Field(20, ge=1)
    anticipa_ask_rate_per_day: int = Field(200, ge=1)
    # Resident conversation (ADR-071): own daily cost cap (task engagement.converse, UTC day) and per-user request limit,
    # checked before any model call. Over either limit the deterministic safety path still works; only the model reply stops.
    anticipa_converse_daily_budget_usd: Decimal = Field(Decimal("1.00"), ge=0)
    anticipa_converse_rate_per_hour: int = Field(60, ge=1)

    @property
    def enabled_providers(self) -> frozenset[str]:
        return frozenset({"anthropic", "nebius"} if self.anticipa_enable_nebius else {"anthropic"})

    @field_validator("anthropic_api_key", "nebius_api_key", "nebius_base_url", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        # .env.example ships "replace-me"; treat that and empty strings as "not configured".
        if isinstance(v, SecretStr):
            v = v.get_secret_value()
        if isinstance(v, str) and v.strip() in _PLACEHOLDERS:
            return None
        return v.strip() if isinstance(v, str) else v


def get_ai_settings() -> AiSettings:
    return AiSettings()
