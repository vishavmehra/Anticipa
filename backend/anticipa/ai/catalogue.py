"""Start-up enforcement: every configured model must exist in the live-verified provider snapshot,
no Meta/llama model may be configured, and only the two approved providers may appear (ADR-021/023)."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

from .config import RouterConfig
from .settings import CONFIG_DIR

log = logging.getLogger("anticipa.ai.catalogue")
ALLOWED_PROVIDERS = frozenset({"anthropic", "nebius"})


class CatalogueError(RuntimeError):
    """Configuration does not match the verified provider catalogue (raised at start-up)."""


def is_meta_model(model_id: str) -> bool:
    """ADR-021: Meta models are excluded. Matches meta-llama/*, meta-models/*, and any *llama* id."""
    org = model_id.split("/", 1)[0].lower()
    return org.startswith("meta") or "llama" in model_id.lower()


def load_snapshot(path: Path | None = None) -> dict:
    path = path or CONFIG_DIR / "provider_catalogue.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CatalogueError(f"cannot read provider catalogue snapshot {path}: {e}") from e


def snapshot_ids(snapshot: dict) -> dict[str, set[str]]:
    return {
        "anthropic": {m["id"] for m in snapshot.get("anthropic", {}).get("models", [])},
        "nebius": {
            m["id"] for m in snapshot.get("nebius", {}).get("models", []) if not is_meta_model(m["id"])
        },
    }


def validate_config(cfg: RouterConfig, snapshot: dict | None = None) -> None:
    """Collect every problem and raise one CatalogueError, so a bad models.toml is fixed in one pass."""
    snapshot = snapshot or load_snapshot(cfg.source_dir / cfg.router.catalogue_snapshot)
    known = snapshot_ids(snapshot)
    problems: list[str] = []

    for name in cfg.providers:
        if name not in ALLOWED_PROVIDERS:
            problems.append(f"provider {name!r} is not permitted (only anthropic, nebius)")

    nebius_prices = {m["id"]: m.get("pricing", {}) for m in snapshot.get("nebius", {}).get("models", [])}
    for m in cfg.models:
        ref = f"{m.provider}:{m.id}"
        if m.kind == "embedding" and m.provider != "nebius":
            problems.append(f"{ref}: only Nebius offers embeddings in this architecture")
        if is_meta_model(m.id):
            problems.append(f"{ref}: Meta/llama models are excluded from Anticipa")
        elif m.id not in known[m.provider]:
            problems.append(f"{ref}: not in the verified {m.provider} catalogue snapshot")
        elif m.provider == "nebius":
            _warn_on_price_drift(m.id, m.input_per_mtok, m.output_per_mtok, nebius_prices.get(m.id, {}))

    for task, t in cfg.tasks.items():
        for c in t.candidates:
            try:
                model = cfg.model(c.provider, c.model_id)
            except KeyError:
                problems.append(f"task {task}: candidate {c.ref} is not declared in [[models]]")
                continue
            if model.kind != t.kind:
                problems.append(f"task {task}: candidate {c.ref} is kind={model.kind}, task is kind={t.kind}")
            if t.kind == "embedding":
                if not t.dimensions:
                    problems.append(f"task {task}: embedding tasks need `dimensions`")
                elif model.native_dimensions and t.dimensions > model.native_dimensions:
                    problems.append(
                        f"task {task}: dimensions {t.dimensions} > native {model.native_dimensions}"
                    )
    if problems:
        raise CatalogueError("invalid models.toml:\n  - " + "\n  - ".join(problems))


def _warn_on_price_drift(model_id: str, in_mtok: Decimal, out_mtok: Decimal, pricing: dict) -> None:
    try:
        live_in = Decimal(str(pricing["prompt"])) * 1_000_000
        live_out = Decimal(str(pricing["completion"])) * 1_000_000
    except (KeyError, ArithmeticError):
        return
    if live_in != in_mtok or live_out != out_mtok:
        log.warning(
            "price drift for %s: toml %s/%s vs snapshot %s/%s", model_id, in_mtok, out_mtok, live_in, live_out
        )
