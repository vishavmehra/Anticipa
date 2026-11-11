# /// script
# requires-python = ">=3.11"
# dependencies = ["anthropic", "httpx"]
# ///
"""Verify Anthropic + Nebius credentials and live model catalogues (ADR-023).

Reads keys from the repo-root .env and never prints them. Writes a non-secret
snapshot to config/provider_catalogue.json. With live tests enabled it makes three
tiny paid calls (one Claude message, one Nebius chat, one Nebius embedding batch).

Usage:  uv run scripts/check_providers.py [--no-live]
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx

ROOT = Path(__file__).resolve().parents[1]
NEBIUS_CANDIDATE_URLS = [
    "https://api.tokenfactory.nebius.com/v1/",
    "https://api.studio.nebius.com/v1/",
]
EMBED_TARGET_DIMS = 1024  # pgvector HNSW limit: 2000 (vector) / 4000 (halfvec); Qwen3-Embedding is MRL-trained


def is_meta_model(model_id: str) -> bool:
    """ADR-021: Meta models are excluded. Matches meta-llama/*, meta-models/*, and any *llama* id."""
    org = model_id.split("/", 1)[0].lower()
    return org.startswith("meta") or "llama" in model_id.lower()


PROBE = "Reply with exactly: OK"
EMBED_PROBE = [
    "Mother was diagnosed with breast cancer at age 45.",
    "माँ को 45 वर्ष की आयु में स्तन कैंसर का निदान हुआ था।",
    "تم تشخيص الأم بسرطان الثدي في سن 45.",
    "The clinic opens at nine in the morning.",
]


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def check_anthropic(key: str, live: bool) -> dict:
    out: dict = {"key_prefix": key[:11], "auth_mode": None, "models": [], "live_test": None, "errors": []}
    client = None
    for mode in ("x-api-key", "bearer"):
        try:
            if mode == "x-api-key":
                client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=30)
            else:
                client = anthropic.Anthropic(
                    auth_token=key, max_retries=1, timeout=30,
                    default_headers={"anthropic-beta": "oauth-2025-04-20"},
                )
            out["models"] = [
                {
                    "id": m.id,
                    "display_name": getattr(m, "display_name", None),
                    "max_input_tokens": getattr(m, "max_input_tokens", None),
                    "max_output_tokens": getattr(m, "max_tokens", None),
                }
                for m in client.models.list()
            ]
            out["auth_mode"] = mode
            break
        except anthropic.AuthenticationError:
            out["errors"].append(f"{mode}: 401 authentication_error")
        except anthropic.APIStatusError as e:
            out["errors"].append(f"{mode}: HTTP {e.status_code}: {str(e.message)[:200]}")
        except anthropic.APIConnectionError as e:
            out["errors"].append(f"{mode}: connection error: {type(e).__name__}")
            break
    if live and out["auth_mode"] and client is not None:
        ids = {m["id"] for m in out["models"]}
        model = next((m for m in ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5") if m in ids), None)
        if model:
            t0 = time.perf_counter()
            try:
                r = client.messages.create(model=model, max_tokens=256, messages=[{"role": "user", "content": PROBE}])
                out["live_test"] = {
                    "model": r.model,
                    "stop_reason": r.stop_reason,
                    "input_tokens": r.usage.input_tokens,
                    "output_tokens": r.usage.output_tokens,
                    "latency_ms": round((time.perf_counter() - t0) * 1000),
                    "text": "".join(b.text for b in r.content if b.type == "text")[:40],
                }
            except anthropic.APIStatusError as e:
                out["live_test"] = {"model": model, "error": f"HTTP {e.status_code}: {str(e.message)[:200]}"}
    return out


def check_nebius(key: str, base_override: str | None, live: bool) -> dict:
    out: dict = {"base_url": None, "tried": [], "models": [], "excluded_meta": [],
                 "chat_test": None, "embedding_test": None}
    urls = list(dict.fromkeys([u for u in [base_override, *NEBIUS_CANDIDATE_URLS] if u]))
    with httpx.Client(timeout=60, headers={"Authorization": f"Bearer {key}"}) as http:
        raw: list[dict] = []
        for base in urls:
            try:
                r = http.get(base.rstrip("/") + "/models", params={"verbose": "true"})
            except httpx.HTTPError as e:
                out["tried"].append({"url": base, "error": type(e).__name__})
                continue
            out["tried"].append({"url": base, "status": r.status_code})
            if r.status_code == 200:
                out["base_url"] = base
                raw = r.json().get("data", [])
                break
        if not out["base_url"]:
            return out
        base = out["base_url"].rstrip("/")
        for m in raw:
            mid = m.get("id", "")
            (out["excluded_meta"] if is_meta_model(mid) else out["models"]).append(m)
        ids = [m["id"] for m in out["models"]]
        emb_ids = [i for i in ids if "embed" in i.lower()]
        chat_ids = [i for i in ids if i not in emb_ids]
        out["embedding_candidates"] = emb_ids
        if not live:
            return out
        chat_pref = ("Qwen3-30B-A3B-Instruct", "Qwen3-235B-A22B-Instruct", "Qwen3-32B", "Qwen")
        chat_model = next((i for p in chat_pref for i in chat_ids if p.lower() in i.lower()), chat_ids[0] if chat_ids else None)
        if chat_model:
            t0 = time.perf_counter()
            r = http.post(f"{base}/chat/completions", json={
                "model": chat_model, "max_tokens": 64,
                "messages": [{"role": "user", "content": PROBE}],
            })
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            out["chat_test"] = {
                "model": chat_model, "status": r.status_code,
                "latency_ms": round((time.perf_counter() - t0) * 1000),
                "usage": body.get("usage"),
                "text": (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")[:40],
                "error": None if r.status_code == 200 else str(body)[:300],
            }
        emb_model = next((i for i in emb_ids if "qwen3-embedding-8b" in i.lower()), emb_ids[0] if emb_ids else None)
        if emb_model:
            t0 = time.perf_counter()
            r = http.post(f"{base}/embeddings", json={"model": emb_model, "input": EMBED_PROBE})
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            vecs = [d["embedding"] for d in body.get("data", [])] if r.status_code == 200 else []
            out["embedding_test"] = {
                "model": emb_model, "status": r.status_code,
                "latency_ms": round((time.perf_counter() - t0) * 1000),
                "dimensions": len(vecs[0]) if vecs else None,
                "usage": body.get("usage"),
                "cosine_en_hi": round(cosine(vecs[0], vecs[1]), 4) if len(vecs) == 4 else None,
                "cosine_en_ar": round(cosine(vecs[0], vecs[2]), 4) if len(vecs) == 4 else None,
                "cosine_en_unrelated": round(cosine(vecs[0], vecs[3]), 4) if len(vecs) == 4 else None,
                "error": None if r.status_code == 200 else str(body)[:300],
            }
            # Does the endpoint honour the OpenAI-style `dimensions` parameter (Matryoshka truncation)?
            r = http.post(f"{base}/embeddings", json={"model": emb_model, "input": EMBED_PROBE[:2],
                                                      "dimensions": EMBED_TARGET_DIMS})
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            vecs = [d["embedding"] for d in body.get("data", [])] if r.status_code == 200 else []
            out["embedding_test"]["dimensions_param"] = {
                "requested": EMBED_TARGET_DIMS, "status": r.status_code,
                "returned_dimensions": len(vecs[0]) if vecs else None,
                "cosine_en_hi": round(cosine(vecs[0], vecs[1]), 4) if len(vecs) == 2 else None,
                "error": None if r.status_code == 200 else str(body)[:300],
            }
    return out


def main() -> int:
    live = "--no-live" not in sys.argv
    env = load_env(ROOT / ".env")
    a_key, n_key = env.get("ANTHROPIC_API_KEY", ""), env.get("NEBIUS_API_KEY", "")
    snapshot = {
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Non-secret snapshot of live provider catalogues. Regenerate with scripts/check_providers.py.",
        "anthropic": check_anthropic(a_key, live) if a_key else {"error": "ANTHROPIC_API_KEY missing"},
        "nebius": check_nebius(n_key, env.get("NEBIUS_BASE_URL"), live) if n_key else {"error": "NEBIUS_API_KEY missing"},
    }
    dest = ROOT / "config" / "provider_catalogue.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")

    a, n = snapshot["anthropic"], snapshot["nebius"]
    print("ANTHROPIC auth_mode:", a.get("auth_mode"), "| key prefix:", a.get("key_prefix"), "| errors:", a.get("errors"))
    print("ANTHROPIC models:", [m["id"] for m in a.get("models", [])])
    print("ANTHROPIC live:", a.get("live_test"))
    print("NEBIUS base_url:", n.get("base_url"), "| tried:", n.get("tried"))
    print("NEBIUS models (non-Meta):", len(n.get("models", [])), "| Meta excluded:", len(n.get("excluded_meta", [])))
    print("NEBIUS ids:", [m["id"] for m in n.get("models", [])])
    print("NEBIUS embedding candidates:", n.get("embedding_candidates"))
    print("NEBIUS chat:", n.get("chat_test"))
    print("NEBIUS embed:", n.get("embedding_test"))
    print("snapshot ->", dest.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
