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
    anticipa_llm_cache: bool = False  # replay cache for evaluation runs (off by default)
    anticipa_llm_cache_dir: Path = REPO_ROOT / "data" / "cache" / "llm"

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
