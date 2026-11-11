"""Provider health: models-list calls only (free, no tokens). Used by /integrations and operator scripts."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .claude import ClaudeAdapter
from .config import RouterConfig, load_config
from .nebius import NebiusAdapter
from .settings import AiSettings, get_ai_settings
from .types import RouterError


@dataclass
class ProviderHealth:
    provider: str
    status: str  # ok | degraded | not_configured | <RouterError category>
    latency_ms: int | None = None
    model_count: int | None = None
    missing_models: list[str] = field(default_factory=list)  # configured ids absent from the live list
    detail: str | None = None


async def check_providers(
    settings: AiSettings | None = None, config: RouterConfig | None = None
) -> dict[str, ProviderHealth]:
    settings = settings or get_ai_settings()
    config = config or load_config()
    out: dict[str, ProviderHealth] = {}

    async def probe(name: str, adapter: ClaudeAdapter | NebiusAdapter | None, key_env: str) -> None:
        if adapter is None:
            out[name] = ProviderHealth(name, "not_configured", detail=f"{key_env} is not set")
            return
        started = time.perf_counter()
        try:
            ids = set(await adapter.list_model_ids())
        except RouterError as e:
            out[name] = ProviderHealth(name, e.category, detail=e.message)
        else:
            wanted = {m.id for m in config.models if m.provider == name}
            missing = sorted(wanted - ids)
            out[name] = ProviderHealth(
                name,
                "degraded" if missing else "ok",
                round((time.perf_counter() - started) * 1000),
                len(ids),
                missing,
                f"{len(missing)} configured model(s) not listed" if missing else None,
            )
        finally:
            if adapter is not None:
                await adapter.aclose()

    claude = (
        ClaudeAdapter(settings.anthropic_api_key.get_secret_value(), config.provider("anthropic"))
        if settings.anthropic_api_key
        else None
    )
    base = settings.nebius_base_url or config.provider("nebius").base_url
    nebius = (
        NebiusAdapter(settings.nebius_api_key.get_secret_value(), base, config.provider("nebius"))
        if settings.nebius_api_key and base
        else None
    )
    await probe("anthropic", claude, "ANTHROPIC_API_KEY")
    await probe("nebius", nebius, "NEBIUS_API_KEY")
    return out
