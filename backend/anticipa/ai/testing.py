"""Test helpers for code that depends on the Model Router (no network, no keys, no sleeping).

    adapter = FakeAdapter(ok('{"facts": []}'), err("rate_limited", retry_after=1))
    router, sink, sleeps = make_test_router({"nebius": adapter})

Each FakeAdapter call pops the next scripted item: an AdapterResponse is returned, an exception is raised.
Used by anticipa's own tests; import it from other modules' tests instead of re-mocking providers.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .cache import ResponseCache
from .config import Candidate, RouterConfig, load_config
from .router import ModelRouter
from .sinks import InMemorySink, InvocationSink
from .types import AdapterResponse, RouterError, Usage


class FakeAdapter:
    def __init__(self, *script: AdapterResponse | Exception) -> None:
        self.script = list(script)
        self.calls: list[Any] = []
        self.embed_calls: list[list[str]] = []

    async def generate(self, call: Any) -> AdapterResponse:
        self.calls.append(call)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def embed(self, model: Any, texts: list[str], dimensions: int) -> tuple[list[list[float]], Usage]:
        self.embed_calls.append(list(texts))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return [[0.0] * dimensions for _ in texts], item.usage

    async def aclose(self) -> None:
        return None


def ok(text: str = "", *, i: int = 100, o: int = 50, cr: int = 0, cw: int = 0) -> AdapterResponse:
    return AdapterResponse(
        text=text, usage=Usage(input_tokens=i, output_tokens=o, cache_read_tokens=cr, cache_write_tokens=cw)
    )


def err(category: str, **kw: Any) -> RouterError:
    return RouterError(category, f"scripted {category}", **kw)  # type: ignore[arg-type]


def two_provider_config() -> RouterConfig:
    """Shipped models.toml with `evidence.extract` re-pointed at Nebius -> Haiku. The shipped route is
    Haiku-only
    (no Nebius unless opted in), so tests of router MECHANICS (fallback, retries, repair, budget) use
    this one."""
    cfg = load_config()
    route = cfg.tasks["evidence.extract"]
    route.candidates = [
        Candidate(provider="nebius", model_id="Qwen/Qwen3-235B-A22B-Instruct-2507"),
        Candidate(provider="anthropic", model_id="claude-haiku-5-5"),
    ]
    route.temperature = 0.0
    return cfg


def make_test_router(
    adapters: dict[str, Any],
    *,
    config: RouterConfig | None = None,
    cache: ResponseCache | None = None,
    sink: InvocationSink | None = None,
    daily_budget_usd: Decimal | None = None,
    run_budget_usd: Decimal | None = None,
    enabled_providers: frozenset[str] | None = None,
    provider_credit_usd: dict[str, Decimal] | None = None,
) -> tuple[ModelRouter, Any, list[float]]:
    """Router over the real models.toml routes with scripted adapters: (router, sink, sleeps)."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    sink = sink or InMemorySink()
    router = ModelRouter(
        config or load_config(),
        adapters,
        sink,
        cache=cache,
        sleep=fake_sleep,
        jitter=lambda: 1.0,
        daily_budget_usd=daily_budget_usd,
        run_budget_usd=run_budget_usd,
        enabled_providers=enabled_providers,
        provider_credit_usd=provider_credit_usd,
    )
    return router, sink, sleeps
