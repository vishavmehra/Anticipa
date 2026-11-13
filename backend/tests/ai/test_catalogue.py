"""Start-up enforcement (catalogue ids, Meta exclusion, approved providers), settings, prompts, sinks."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from anticipa.ai.catalogue import CatalogueError, is_meta_model, load_snapshot, validate_config
from anticipa.ai.config import RouterConfig, load_config
from anticipa.ai.prompts import PromptNotFound, PromptRegistry, get_prompt
from anticipa.ai.router import build_router
from anticipa.ai.settings import AiSettings
from anticipa.ai.sinks import DbSink, InvocationRecord


def cfg_with(**patch) -> RouterConfig:
    data = load_config().model_dump(mode="python")
    for key, value in patch.items():
        data[key] = value
    return RouterConfig.model_validate(data)


def model_entry(provider="nebius", id="Qwen/Qwen3-30B-A3B-Instruct-2507", kind="chat", **kw):
    return {
        "provider": provider, "id": id, "kind": kind, "input_per_mtok": "0.1", "output_per_mtok": "0.3",
        "verified_on": "2026-11-06", **kw,
    }  # fmt: skip


def test_shipped_models_toml_matches_the_verified_catalogue():
    cfg = load_config()
    validate_config(cfg)
    assert {"evidence.extract", "evidence.extract.complex", "evidence.review", "engagement.draft",
            "embedding.document"} <= set(cfg.tasks)  # fmt: skip
    # owner decision 2026-11-11: Haiku for bulk extraction (no Nebius fallback), Sonnet only by escalation
    assert [c.ref for c in cfg.tasks["evidence.extract"].candidates] == ["anthropic:claude-haiku-5-5"]
    assert [c.ref for c in cfg.tasks["evidence.extract.complex"].candidates] == [
        "anthropic:claude-sonnet-5-5"
    ]
    assert [c.ref for c in cfg.tasks["engagement.draft"].candidates] == ["anthropic:claude-haiku-5-5"]
    nebius_routes = {t for t, r in cfg.tasks.items() if any(c.provider == "nebius" for c in r.candidates)}
    assert nebius_routes == {"embedding.document"}  # the only Nebius route, and it is opt-in
    assert cfg.tasks["embedding.document"].dimensions == 1024


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("meta-models/muse-spark-open", True),
        ("meta-llama/Llama-3.3-70B-Instruct", True),
        ("nvidia/Llama-3.1-Nemotron-70B", True),  # any *llama* id is excluded
        ("Qwen/Qwen3-235B-A22B-Instruct-2507", False),
        ("claude-opus-5-5", False),
    ],
)
def test_is_meta_model(model_id, expected):
    assert is_meta_model(model_id) is expected


def test_meta_model_in_config_is_rejected_even_if_listed_in_snapshot():
    cfg = cfg_with(
        models=[*load_config().model_dump()["models"], model_entry(id="meta-llama/Llama-3.3-70B-Instruct")]
    )
    with pytest.raises(CatalogueError, match="Meta/llama models are excluded"):
        validate_config(cfg)


def test_model_missing_from_verified_catalogue_is_rejected():
    cfg = cfg_with(models=[*load_config().model_dump()["models"], model_entry(id="Qwen/Not-A-Real-Model")])
    with pytest.raises(CatalogueError, match="not in the verified nebius catalogue"):
        validate_config(cfg)
    cfg = cfg_with(models=[model_entry(provider="anthropic", id="claude-made-up-9")])
    with pytest.raises(CatalogueError, match="anthropic:claude-made-up-9"):
        validate_config(cfg)


def test_only_two_providers_are_allowed():
    with pytest.raises(ValidationError):  # Literal["anthropic", "nebius"] on [[models]].provider
        cfg_with(models=[model_entry(provider="openai", id="gpt-x")])
    cfg = cfg_with(providers={"openai": {"timeout_s": 5}})
    with pytest.raises(CatalogueError, match="provider 'openai' is not permitted"):
        validate_config(cfg)


def test_route_problems_are_all_reported_at_once():
    tasks = {
        "t.undeclared": {"kind": "chat", "candidates": ["nebius:Qwen/Qwen3-30B-A3B-Instruct-2507"]},
        "t.kind": {"kind": "chat", "candidates": ["nebius:Qwen/Qwen3-Embedding-8B"]},
        "t.dims": {"kind": "embedding", "candidates": ["nebius:Qwen/Qwen3-Embedding-8B"], "dimensions": 9999},
    }
    base = [m for m in load_config().model_dump()["models"] if m["id"] != "Qwen/Qwen3-30B-A3B-Instruct-2507"]
    with pytest.raises(CatalogueError) as ei:
        validate_config(cfg_with(models=base, tasks=tasks))
    msg = str(ei.value)
    assert (
        "not declared in [[models]]" in msg
        and "is kind=embedding" in msg
        and "dimensions 9999 > native 4096" in msg
    )


def test_embeddings_only_on_nebius():
    cfg = cfg_with(
        models=[model_entry(provider="anthropic", id="claude-haiku-5-5", kind="embedding")], tasks={}
    )
    with pytest.raises(CatalogueError, match="only Nebius offers embeddings"):
        validate_config(cfg)


def test_bad_candidate_syntax_is_rejected():
    with pytest.raises(ValidationError, match="provider:model_id"):
        cfg_with(tasks={"t": {"kind": "chat", "candidates": ["just-a-model-id"]}})


def test_snapshot_is_readable_and_unreadable_snapshot_fails_fast(tmp_path):
    assert "anthropic" in load_snapshot()
    with pytest.raises(CatalogueError, match="cannot read"):
        load_snapshot(tmp_path / "missing.json")


# ------------------------------------------------------------------------------------ settings / build
def test_settings_treat_placeholders_as_unset_and_hide_secrets():
    s = AiSettings(_env_file=None, anthropic_api_key="replace-me", nebius_api_key="  ")
    assert s.anthropic_api_key is None and s.nebius_api_key is None
    s = AiSettings(_env_file=None, anthropic_api_key="sk-ant-secret-value")
    assert "sk-ant-secret-value" not in repr(s) and "sk-ant-secret-value" not in str(s.anthropic_api_key)


def test_daily_budget_default_env_override_and_validation(monkeypatch):
    monkeypatch.delenv("ANTICIPA_AI_DAILY_BUDGET_USD", raising=False)
    assert AiSettings(_env_file=None).anticipa_ai_daily_budget_usd == Decimal("5.00")
    monkeypatch.setenv("ANTICIPA_AI_DAILY_BUDGET_USD", "1.25")
    assert AiSettings(_env_file=None).anticipa_ai_daily_budget_usd == Decimal("1.25")
    monkeypatch.setenv("ANTICIPA_AI_DAILY_BUDGET_USD", "-1")
    with pytest.raises(ValidationError):
        AiSettings(_env_file=None)


def test_build_router_wires_adapters_budget_and_enforces_catalogue():
    s = AiSettings(
        _env_file=None,
        anthropic_api_key="sk-ant-x",
        nebius_api_key="n-x",
        anticipa_enable_nebius=True,
        anticipa_ai_daily_budget_usd="2.5",
    )
    router = build_router(settings=s)
    assert set(router.adapters) == {"anthropic", "nebius"} and router.daily_budget_usd == Decimal("2.5")
    assert router.provider_credit_usd == {"nebius": Decimal("50.00")} and router.run_budget_usd is None
    keyless = build_router(settings=AiSettings(_env_file=None))  # no keys: builds, calls fail at call time
    assert keyless.adapters == {}
    bad = cfg_with(models=[model_entry(id="Qwen/Not-A-Real-Model")])
    with pytest.raises(CatalogueError):
        build_router(settings=s, config=bad)


def test_nebius_is_off_by_default_even_with_a_key_and_the_response_cache_is_on():
    s = AiSettings(_env_file=None, anthropic_api_key="sk-ant-x", nebius_api_key="n-x")
    assert s.anticipa_enable_nebius is False and s.enabled_providers == {"anthropic"}
    router = build_router(settings=s)
    assert set(router.adapters) == {"anthropic"}  # no Nebius adapter exists at all
    assert router.cache is not None  # every successful output is cached by default
    assert build_router(settings=s, run_budget=True).run_budget_usd == Decimal("40.00")
    assert AiSettings(_env_file=None).anticipa_nebius_credit_usd == Decimal("50.00")
    assert AiSettings(_env_file=None).anticipa_nebius_run_approved_usd is None
    assert AiSettings(_env_file=None, anticipa_llm_cache=False).anticipa_llm_cache is False


# ------------------------------------------------------------------------------------ prompts
def test_prompt_registry_hashes_content_and_blocks_traversal(tmp_path):
    p = get_prompt("evidence.extract@1")
    assert p.ref == "evidence.extract@1" and len(p.sha256) == 64 and "untrusted" in p.text
    assert get_prompt("evidence.extract@1").sha256 == p.sha256
    for bad in ("../../etc/passwd@1", "evidence.extract", "evidence.extract@x", "Evidence.Extract@1"):
        with pytest.raises(PromptNotFound):
            get_prompt(bad)
    (tmp_path / "a.b@2.txt").write_bytes(
        b"line1\r\nline2  \r\n"
    )  # bytes: write_text re-translates \n on Windows
    reg = PromptRegistry([tmp_path])
    assert reg.get("a.b@2").text == "line1\nline2\n"  # CRLF-normalised so hashes are stable across OSes
    with pytest.raises(PromptNotFound):
        reg.get("a.b@3")


# ------------------------------------------------------------------------------------ DB sink
def test_invocation_row_matches_the_model_invocations_columns():
    models = pytest.importorskip("anticipa.ai.models")  # foundation workstream's table
    base = {"request_id": "r", "task": "t", "provider": "p", "model_id": "m", "prompt_version": "v"}
    row = InvocationRecord(id="a" * 32, job_id="not-a-uuid", **base).as_row()
    assert set(row) <= set(models.ModelInvocation.__table__.columns.keys())
    assert row["job_id"] is None and row["cache_hit"] is False


def test_error_category_constraint_covers_all_router_categories():
    from typing import get_args

    from anticipa.ai.types import ErrorCategory

    models = pytest.importorskip("anticipa.ai.models")
    assert set(get_args(ErrorCategory)) <= set(models.ERROR_CATEGORIES)


@pytest.mark.anyio
async def test_db_sink_writes_and_sums_todays_spend_via_lazy_model_import(monkeypatch):
    from sqlalchemy import Column, DateTime, Integer, Numeric, create_engine
    from sqlalchemy.orm import Session, declarative_base
    from sqlalchemy.pool import StaticPool

    Base = declarative_base()

    class ModelInvocation(Base):  # minimal stand-in with the columns the sink touches
        __tablename__ = "model_invocations"
        pk = Column(Integer, primary_key=True, autoincrement=True)
        cost_usd = Column(Numeric(12, 6))
        created_at = Column(DateTime(timezone=True))

        def __init__(self, **kw):  # accept the full as_row() payload, keep what we model
            super().__init__(cost_usd=kw["cost_usd"], created_at=kw["created_at"])

    monkeypatch.setitem(
        sys.modules, "anticipa.ai.models", types.SimpleNamespace(ModelInvocation=ModelInvocation)
    )
    # StaticPool + check_same_thread=False: the sink hops to a worker thread (asyncio.to_thread) and an
    # in-memory sqlite database exists only per connection.
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sink = DbSink(lambda: Session(engine))
    now = datetime.now(UTC)
    base = {"request_id": "r", "task": "t", "provider": "p", "model_id": "m", "prompt_version": "v"}
    await sink.record(InvocationRecord(id="b" * 32, cost_usd=Decimal("1.25"), created_at=now, **base))
    yesterday = now - timedelta(days=1)
    await sink.record(InvocationRecord(id="c" * 32, cost_usd=Decimal("9"), created_at=yesterday, **base))
    assert await sink.spent_today_usd() == Decimal("1.25")


def test_escalation_share_defaults_to_the_ceiling_and_can_never_exceed_it():
    assert AiSettings(_env_file=None).anticipa_ai_escalation_share == Decimal("0.10")
    assert AiSettings(
        _env_file=None, anticipa_ai_escalation_share="0.02"
    ).anticipa_ai_escalation_share == Decimal("0.02")
    with pytest.raises(ValidationError):
        AiSettings(_env_file=None, anticipa_ai_escalation_share="0.11")
    with pytest.raises(ValidationError):
        AiSettings(_env_file=None, anticipa_ai_escalation_share="-0.01")
