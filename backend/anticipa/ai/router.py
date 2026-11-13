"""Model Router: the only path to Claude/Nebius (ADR-022).

Per task: ordered candidates -> per-candidate bounded retries (backoff + jitter, honours retry-after) ->
schema validation with a bounded repair turn -> fallback to the next candidate, which is validated again.
A refusal never falls back (a safety decision, not an outage). One accounting record per candidate attempt.
Clinical eligibility is never decided here: the router only returns validated model output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import ValidationError

from .cache import ResponseCache
from .catalogue import validate_config
from .claude import ClaudeAdapter
from .config import Candidate, ModelConfig, RouterConfig, TaskConfig, load_config
from .nebius import NebiusAdapter
from .settings import AiSettings, get_ai_settings
from .sinks import InMemorySink, InvocationRecord, InvocationSink
from .types import (
    AdapterCall,
    EmbedRequest,
    EmbedResult,
    GenerateRequest,
    GenerateResult,
    RouterError,
    Usage,
)

log = logging.getLogger("anticipa.ai.router")
T = TypeVar("T")
_NO_COST = Decimal("0")


def _estimate_input_tokens(*parts: str) -> int:
    """Deliberately pessimistic (UTF-8 bytes / 2 ~ 2x English, ~1.5x Devanagari) for the spend guard only."""
    return -(-sum(len(p.encode("utf-8")) for p in parts) // 2)


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _json_text(text: str) -> str:
    """Strip a ```json fence some open models add even in JSON mode; anything else is left for validation."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


def _error_summary(e: ValidationError | ValueError) -> str:
    """Field paths and messages only: never echoes input values (they may contain document text)."""
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:8])
    return "output is not valid JSON"


class ModelRouter:
    def __init__(
        self,
        config: RouterConfig,
        adapters: dict[str, Any],
        sink: InvocationSink,
        *,
        cache: ResponseCache | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        daily_budget_usd: Decimal | None = None,
        run_budget_usd: Decimal | None = None,
        initial_run_spend: Decimal = _NO_COST,
        enabled_providers: frozenset[str] | None = None,
        provider_credit_usd: dict[str, Decimal] | None = None,
    ) -> None:
        self.config = config
        self.adapters = adapters
        self.sink = sink
        self.cache = cache
        self._sleep = sleep
        self._jitter = jitter
        self._sems = {name: asyncio.Semaphore(p.max_concurrency) for name, p in config.providers.items()}
        self.daily_budget_usd = daily_budget_usd  # None = no guard (tests, offline tools)
        self._reserved = Decimal(0)  # in-flight worst-case reservations + completed-but-unrecorded cost
        # Per-run cap: this router instance is one run (a job). `_run_spent` is everything billed so far
        # (seeded with an earlier attempt's spend when a job resumes); `_inflight` the open reservations.
        self.run_budget_usd = run_budget_usd
        self._run_spent = initial_run_spend
        self._inflight = Decimal(0)
        # None = every provider; otherwise candidates of other providers are dropped at routing time.
        self.enabled_providers = enabled_providers
        # Lifetime per-provider credit ledger (recorded spend + this router's in-flight/unrecorded cost).
        self.provider_credit_usd = provider_credit_usd or {}
        self._held: dict[str, Decimal] = defaultdict(Decimal)  # per-provider in-flight reservations + pending

    # ------------------------------------------------------------------ routing helpers
    def _route(self, task: str, kind: str) -> TaskConfig:
        route = self.config.tasks.get(task)
        if route is None:
            raise RouterError("invalid_request", f"unknown task {task!r}")
        if route.kind != kind:
            raise RouterError("invalid_request", f"task {task!r} is a {route.kind} task, not {kind}")
        if self.enabled_providers is None:
            return route
        allowed = [c for c in route.candidates if c.provider in self.enabled_providers]
        if not allowed:
            raise RouterError(
                "invalid_request",
                f"task {task!r} has no enabled provider (candidates: "
                f"{', '.join(c.ref for c in route.candidates)}; Nebius needs ANTICIPA_ENABLE_NEBIUS=1)",
            )
        return route.model_copy(update={"candidates": allowed})

    @property
    def run_spent_usd(self) -> Decimal:
        return self._run_spent

    def _adapter(self, cand: Candidate) -> Any:
        adapter = self.adapters.get(cand.provider)
        if adapter is None:
            raise RouterError(
                "auth",
                f"{cand.provider} is not configured (API key missing)",
                provider=cand.provider,
                model_id=cand.model_id,
            )
        return adapter

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        r = self.config.router
        backoff = min(r.backoff_max_s, r.backoff_base_s * 2**attempt) * (0.5 + 0.5 * self._jitter())
        if retry_after is not None:
            return max(min(retry_after, r.retry_after_cap_s), backoff)
        return backoff

    @asynccontextmanager
    async def _reserve(self, est: Decimal, cand: Candidate) -> AsyncIterator[None]:
        """Spend guards: refuse the provider call if this run's spend + everything in flight + this
        call's worst
        case would exceed the per-run budget, or if recorded spend today + the same would exceed the daily
        budget. Reserve first, then read the sink, so concurrent callers see each other. If today's spend
        cannot be read the guard fails closed."""
        self._reserved += est
        self._inflight += est
        self._held[cand.provider] += est
        try:
            kw: dict[str, Any] = {"provider": cand.provider, "model_id": cand.model_id, "retryable": False}
            credit = self.provider_credit_usd.get(cand.provider)
            if credit is not None:
                try:
                    spent_p = await self.sink.spent_total_usd(cand.provider)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "cannot read %s spend; refusing the provider call (fail closed)", cand.provider
                    )
                    raise RouterError(
                        "budget_exceeded", f"{cand.provider} credit ledger could not be read", **kw
                    ) from None
                if spent_p + self._held[cand.provider] > credit:
                    raise RouterError(
                        "budget_exceeded",
                        f"{cand.provider} credit ${credit} would be exceeded "
                        f"(spent ${spent_p:.4f}, this call up to ${est:.4f})",
                        **kw,
                    )
            if self.run_budget_usd is not None and self._run_spent + self._inflight > self.run_budget_usd:
                log.warning(
                    "per-run AI budget guard tripped: spent=%s in_flight=%s budget=%s",
                    self._run_spent,
                    self._inflight - est,
                    self.run_budget_usd,
                )
                raise RouterError(
                    "budget_exceeded",
                    f"per-run AI budget ${self.run_budget_usd} would be exceeded "
                    f"(run spent ${self._run_spent:.4f}, this call up to ${est:.4f})",
                    **kw,
                )
            if self.daily_budget_usd is not None:
                try:
                    spent = await self.sink.spent_today_usd()
                except Exception:  # noqa: BLE001
                    log.exception("cannot read today's AI spend; refusing the provider call (fail closed)")
                    raise RouterError(
                        "budget_exceeded", "today's AI spend could not be verified", **kw
                    ) from None
                if spent + self._reserved > self.daily_budget_usd:
                    log.warning(
                        "daily AI budget guard tripped: spent=%s in_flight=%s this_call<=%s budget=%s",
                        spent,
                        self._reserved - est,
                        est,
                        self.daily_budget_usd,
                    )
                    raise RouterError(
                        "budget_exceeded",
                        f"daily AI budget ${self.daily_budget_usd} would be exceeded "
                        f"(spent ${spent:.4f}, in flight ${self._reserved - est:.4f}, "
                        f"this call up to ${est:.4f})",
                        **kw,
                    )
            yield
        finally:
            self._reserved -= est
            self._inflight -= est
            self._held[cand.provider] -= est

    async def _guarded_generate(
        self, adapter: Any, cand: Candidate, model: ModelConfig, call: AdapterCall
    ) -> Any:
        cap = (
            adapter.effective_max_tokens(call)
            if hasattr(adapter, "effective_max_tokens")
            else call.max_output_tokens
        )
        tokens = _estimate_input_tokens(
            call.system,
            *(m["content"] for m in call.messages),
            json.dumps(call.schema) if call.schema else "",
        )
        async with self._reserve(model.worst_case_cost(tokens, cap), cand):
            return await adapter.generate(call)

    async def _guarded_embed(
        self, adapter: Any, cand: Candidate, model: ModelConfig, batch: list[str], dims: int
    ) -> Any:
        async with self._reserve(model.worst_case_cost(_estimate_input_tokens(*batch), 0), cand):
            return await adapter.embed(model, batch, dims)

    async def _with_retries(self, provider: str, fn: Callable[[], Awaitable[T]]) -> tuple[T, int]:
        """Run fn with bounded retries on transient errors. Returns (result, retries_used)."""
        max_retries = self.config.provider(provider).max_retries
        sem = self._sems.get(provider)
        attempt = 0
        while True:
            try:
                if sem is None:
                    return await fn(), attempt
                async with sem:
                    return await fn(), attempt
            except RouterError as e:
                if not e.retryable or attempt >= max_retries:
                    e.retries_used = attempt
                    raise
                await self._sleep(self._delay(attempt, e.retry_after))
                attempt += 1

    async def _record(self, rec: InvocationRecord) -> None:
        try:
            await self.sink.record(rec)
        except Exception:  # noqa: BLE001 - never lose a paid result to an accounting failure
            log.exception(
                "invocation sink failed: id=%s task=%s model=%s cost_usd=%s",
                rec.id,
                rec.task,
                rec.model_id,
                rec.cost_usd,
            )

    def _failure_record(
        self,
        req_id: str,
        task: str,
        prompt_version: str,
        cand: Candidate,
        model: ModelConfig,
        usage: Usage,
        retries: int,
        started: float,
        err: RouterError,
        idx: int,
        job_id: str | None,
    ) -> InvocationRecord:
        return InvocationRecord(
            id=uuid.uuid4().hex,
            request_id=req_id,
            task=task,
            provider=cand.provider,
            model_id=cand.model_id,
            prompt_version=prompt_version,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=model.cost(usage),
            latency_ms=_ms(started),
            retry_count=retries,
            status="failed",
            validation_status="failed" if err.category == "output_validation" else "not_applicable",
            error_category=err.category,
            fallback_used=idx > 0,
            job_id=job_id,
        )

    # ------------------------------------------------------------------ generate
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        route = self._route(req.task, "chat")
        if not req.messages or req.messages[-1].role != "user":
            raise RouterError("invalid_request", "messages must end with a user turn (no assistant prefill)")
        request_id = uuid.uuid4().hex
        job_id = str(req.metadata["job_id"]) if req.metadata.get("job_id") else None
        schema = req.output_schema.model_json_schema() if req.output_schema else None
        messages = [m.model_dump() for m in req.messages]

        cache_key = None
        if self.cache is not None:
            cache_key = ResponseCache.key(
                req.task, route.model_dump(mode="json"), req.prompt_version, req.system, messages, schema
            )
            hit = await self._from_cache(cache_key, req, request_id, job_id)
            if hit is not None:
                return hit

        last: RouterError | None = None
        dead: set[str] = set()  # providers whose credentials failed in this call
        for idx, cand in enumerate(route.candidates):
            if cand.provider in dead:
                continue
            try:
                result = await self._attempt_generate(
                    req, route, cand, idx, schema, messages, request_id, job_id
                )
            except RouterError as e:
                last = e
                if e.category in ("refusal", "budget_exceeded"):  # not outages: never route around them
                    raise
                if e.category == "auth":
                    dead.add(cand.provider)
                log.warning("candidate failed task=%s %s category=%s", req.task, cand.ref, e.category)
                continue
            if cache_key is not None and self.cache is not None:
                self.cache.put(
                    cache_key,
                    {
                        "text": result.text,
                        "provider": result.provider,
                        "model_id": result.model_id,
                        "usage": result.usage.model_dump(),
                        "cost_usd": str(result.cost_usd),
                    },
                )
            return result
        assert last is not None
        raise last

    async def _from_cache(
        self, key: str, req: GenerateRequest, request_id: str, job_id: str | None
    ) -> GenerateResult | None:
        assert self.cache is not None
        entry = self.cache.get(key)
        if not entry:
            return None
        try:
            output: Any = entry["text"]
            if req.output_schema is not None:
                output = req.output_schema.model_validate_json(
                    _json_text(entry["text"])
                )  # re-validated on replay
            provider, model_id = entry["provider"], entry["model_id"]
        except (KeyError, ValidationError, ValueError):
            return None  # corrupt or stale entry == miss
        inv_id = uuid.uuid4().hex
        await self._record(
            InvocationRecord(
                id=inv_id,
                request_id=request_id,
                task=req.task,
                provider=provider,
                model_id=model_id,
                prompt_version=req.prompt_version,
                status="succeeded",
                cache_hit=True,
                validation_status="valid" if req.output_schema else "not_applicable",
                job_id=job_id,
            )
        )
        return GenerateResult(
            output=output,
            text=entry["text"],
            provider=provider,
            model_id=model_id,
            task=req.task,
            prompt_version=req.prompt_version,
            usage=Usage(),
            cost_usd=_NO_COST,
            latency_ms=0,
            retries=0,
            repairs=0,
            fallback_used=False,
            invocation_id=inv_id,
            request_id=request_id,
            cached=True,
            validation_status="valid" if req.output_schema else "not_applicable",
        )

    async def _attempt_generate(
        self,
        req: GenerateRequest,
        route: TaskConfig,
        cand: Candidate,
        idx: int,
        schema: dict[str, Any] | None,
        messages: list[dict[str, str]],
        request_id: str,
        job_id: str | None,
    ) -> GenerateResult:
        model = self.config.model(cand.provider, cand.model_id)
        started = time.perf_counter()
        usage, retries, repairs = Usage(), 0, 0
        pending = Decimal(0)  # cost of completed calls not yet in the sink; stays reserved until recorded
        call = AdapterCall(
            model=model,
            system=req.system,
            messages=messages,
            schema=schema,
            max_output_tokens=req.max_output_tokens,
            effort=route.effort if cand.provider == "anthropic" else None,
            temperature=route.temperature if cand.provider == "nebius" else None,
        )
        try:
            try:
                adapter = self._adapter(cand)
                while True:
                    resp, used = await self._with_retries(
                        cand.provider, lambda c=call: self._guarded_generate(adapter, cand, model, c)
                    )
                    retries += used
                    usage = usage + resp.usage
                    spent_now = model.cost(resp.usage)
                    self._reserved += spent_now
                    self._run_spent += spent_now
                    self._held[cand.provider] += spent_now
                    pending += spent_now
                    if req.output_schema is None:
                        output: Any = resp.text
                        break
                    try:
                        output = req.output_schema.model_validate_json(_json_text(resp.text))
                        break
                    except (ValidationError, ValueError) as ve:
                        if repairs >= self.config.router.repair_attempts:
                            raise RouterError(
                                "output_validation",
                                f"schema validation failed: {_error_summary(ve)}",
                                provider=cand.provider,
                                model_id=cand.model_id,
                            ) from None
                        repairs += 1
                        call = replace(
                            call,
                            messages=[
                                *messages,
                                {"role": "assistant", "content": resp.text},
                                {
                                    "role": "user",
                                    "content": "Your previous reply failed validation "
                                    f"({_error_summary(ve)}). Reply again with only the corrected JSON "
                                    "object. "
                                    "Do not add facts that are not in the source document.",
                                },
                            ],
                        )
            except RouterError as e:
                e.provider, e.model_id = e.provider or cand.provider, e.model_id or cand.model_id
                if not (
                    e.category == "budget_exceeded" and usage.total_tokens == 0
                ):  # no row per refused call
                    await self._record(
                        self._failure_record(
                            request_id,
                            req.task,
                            req.prompt_version,
                            cand,
                            model,
                            usage,
                            retries + e.retries_used,
                            started,
                            e,
                            idx,
                            job_id,
                        )
                    )
                raise

            status = (
                "repaired" if repairs else ("valid" if req.output_schema is not None else "not_applicable")
            )
            cost = model.cost(usage)
            inv_id = uuid.uuid4().hex
            latency = _ms(started)
            await self._record(
                InvocationRecord(
                    id=inv_id,
                    request_id=request_id,
                    task=req.task,
                    provider=cand.provider,
                    model_id=cand.model_id,
                    prompt_version=req.prompt_version,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    cost_usd=cost,
                    latency_ms=latency,
                    retry_count=retries,
                    status="succeeded",
                    validation_status=status,
                    fallback_used=idx > 0,
                    job_id=job_id,
                )
            )
            return GenerateResult(
                output=output,
                text=resp.text,
                provider=cand.provider,
                model_id=cand.model_id,
                task=req.task,
                prompt_version=req.prompt_version,
                usage=usage,
                cost_usd=cost,
                latency_ms=latency,
                retries=retries,
                repairs=repairs,
                fallback_used=idx > 0,
                invocation_id=inv_id,
                request_id=request_id,
                validation_status=status,
            )
        finally:
            self._reserved -= pending
            self._held[cand.provider] -= pending

    # ------------------------------------------------------------------ embed
    async def embed(self, req: EmbedRequest) -> EmbedResult:
        route = self._route(req.task, "embedding")
        if not req.texts or any(not t.strip() for t in req.texts):
            raise RouterError("invalid_request", "embed requires a non-empty list of non-blank texts")
        request_id = uuid.uuid4().hex
        job_id = str(req.metadata["job_id"]) if req.metadata.get("job_id") else None
        last: RouterError | None = None
        dead: set[str] = set()
        for idx, cand in enumerate(route.candidates):
            if cand.provider in dead:
                continue
            try:
                return await self._attempt_embed(req, route, cand, idx, request_id, job_id)
            except RouterError as e:
                last = e
                if e.category == "budget_exceeded":
                    raise
                if e.category == "auth":
                    dead.add(cand.provider)
        assert last is not None
        raise last

    async def _attempt_embed(
        self,
        req: EmbedRequest,
        route: TaskConfig,
        cand: Candidate,
        idx: int,
        request_id: str,
        job_id: str | None,
    ) -> EmbedResult:
        model = self.config.model(cand.provider, cand.model_id)
        dims = route.dimensions or 0
        started = time.perf_counter()
        usage, retries = Usage(), 0
        pending = Decimal(0)
        vectors: list[list[float]] = []
        try:
            try:
                adapter = self._adapter(cand)
                if not hasattr(adapter, "embed"):
                    raise RouterError(
                        "invalid_request",
                        f"{cand.provider} has no embedding endpoint",
                        provider=cand.provider,
                        model_id=cand.model_id,
                    )
                for i in range(0, len(req.texts), route.batch_size):
                    batch = req.texts[i : i + route.batch_size]
                    (vecs, u), used = await self._with_retries(
                        cand.provider, lambda b=batch: self._guarded_embed(adapter, cand, model, b, dims)
                    )
                    vectors.extend(vecs)
                    usage, retries = usage + u, retries + used
                    spent_now = model.cost(u)
                    self._reserved += spent_now
                    self._run_spent += spent_now
                    self._held[cand.provider] += spent_now
                    pending += spent_now
            except RouterError as e:
                if not (e.category == "budget_exceeded" and usage.total_tokens == 0):
                    await self._record(
                        self._failure_record(
                            request_id,
                            req.task,
                            "n/a",
                            cand,
                            model,
                            usage,
                            retries + e.retries_used,
                            started,
                            e,
                            idx,
                            job_id,
                        )
                    )
                raise
            cost, inv_id, latency = model.cost(usage), uuid.uuid4().hex, _ms(started)
            await self._record(
                InvocationRecord(
                    id=inv_id,
                    request_id=request_id,
                    task=req.task,
                    provider=cand.provider,
                    model_id=cand.model_id,
                    prompt_version="n/a",
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=cost,
                    latency_ms=latency,
                    retry_count=retries,
                    status="succeeded",
                    validation_status="not_applicable",
                    fallback_used=idx > 0,
                    job_id=job_id,
                )
            )
            return EmbedResult(
                vectors=vectors,
                dimensions=dims,
                provider=cand.provider,
                model_id=cand.model_id,
                task=req.task,
                usage=usage,
                cost_usd=cost,
                latency_ms=latency,
                retries=retries,
                fallback_used=idx > 0,
                invocation_id=inv_id,
                request_id=request_id,
            )
        finally:
            self._reserved -= pending
            self._held[cand.provider] -= pending

    async def aclose(self) -> None:
        for adapter in self.adapters.values():
            await adapter.aclose()


# ---------------------------------------------------------------------- construction
def build_router(
    settings: AiSettings | None = None,
    config: RouterConfig | None = None,
    sink: InvocationSink | None = None,
    *,
    run_budget: bool | Decimal = False,
    initial_run_spend: Decimal = _NO_COST,
) -> ModelRouter:
    """Load models.toml, enforce the catalogue (CatalogueError at start-up), wire adapters from .env.

    Nebius is only wired when ANTICIPA_ENABLE_NEBIUS=1; otherwise its candidates are dropped at routing time.
    `run_budget=True` applies ANTICIPA_AI_RUN_BUDGET_USD to this router instance (one job run); a Decimal
    applies that amount instead (the approved limit of a Nebius run)."""
    settings = settings or get_ai_settings()
    config = config or load_config()
    validate_config(config)
    enabled = settings.enabled_providers
    adapters: dict[str, Any] = {}
    if settings.anthropic_api_key:
        adapters["anthropic"] = ClaudeAdapter(
            settings.anthropic_api_key.get_secret_value(), config.provider("anthropic")
        )
    if settings.nebius_api_key and "nebius" in enabled:
        base = settings.nebius_base_url or config.provider("nebius").base_url
        if not base:
            raise RuntimeError("Nebius base URL missing: set NEBIUS_BASE_URL or providers.nebius.base_url")
        adapters["nebius"] = NebiusAdapter(
            settings.nebius_api_key.get_secret_value(), base, config.provider("nebius")
        )
    used = {c.provider for t in config.tasks.values() for c in t.candidates} & enabled
    for missing in sorted(used - adapters.keys()):
        log.warning(
            "provider %s has routes but no API key in .env; those candidates will be skipped", missing
        )
    cache = ResponseCache(settings.anticipa_llm_cache_dir) if settings.anticipa_llm_cache else None
    return ModelRouter(
        config,
        adapters,
        sink or InMemorySink(),
        cache=cache,
        daily_budget_usd=settings.anticipa_ai_daily_budget_usd,
        run_budget_usd=(
            run_budget
            if isinstance(run_budget, Decimal)
            else (settings.anticipa_ai_run_budget_usd if run_budget else None)
        ),
        initial_run_spend=initial_run_spend,
        enabled_providers=enabled,
        provider_credit_usd={
            "nebius": settings.anticipa_nebius_credit_usd,
            **(
                {"anthropic": settings.anticipa_anthropic_spend_cap_usd}
                if settings.anticipa_anthropic_spend_cap_usd is not None
                else {}
            ),
        },
    )


_router: ModelRouter | None = None


def configure_router(sink: InvocationSink | None = None, **kwargs: Any) -> ModelRouter:
    """Called once at application start-up with the DB sink; replaces any previous singleton."""
    global _router
    _router = build_router(sink=sink, **kwargs)
    return _router


def get_router() -> ModelRouter:
    global _router
    if _router is None:
        log.warning("get_router() before configure_router(): using an in-memory invocation sink")
        _router = build_router()
    return _router


__all__ = ["ModelRouter", "build_router", "configure_router", "get_router"]
