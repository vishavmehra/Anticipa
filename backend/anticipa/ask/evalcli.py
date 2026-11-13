"""`python -m anticipa.ask estimate|eval`: cost estimate (no model call) and the live golden-set evaluation.

The golden set (tests/ask/golden.json) stores questions and an operation over a tool result, never expected numbers: the
expected entity and value are computed from the analytics services at run time, then compared with the answer text.
The live run uses the Model Router with a hard run budget, the response cache off (real latency) and the rate limit
bypassed; every call is recorded in model_invocations, so it counts toward the cumulative Anthropic cap."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from anticipa.ai.models import ModelInvocation
from anticipa.ai.router import build_router
from anticipa.ai.settings import REPO_ROOT, get_ai_settings
from anticipa.ai.sinks import DbSink
from anticipa.ask import service, tools
from anticipa.ask.grounding import numbers_in
from anticipa.ask.models import AskRequest
from anticipa.ask.prompts import system_prompt
from anticipa.core.auth import CurrentUser
from anticipa.db import get_sessionmaker

GOLDEN = Path(__file__).resolve().parents[2] / "tests" / "ask" / "golden.json"
TOKENS_PER_CHAR = 0.25
HAIKU_IN, HAIKU_OUT, HAIKU_CACHE_READ = Decimal("0.10"), Decimal("0.50"), Decimal("0.01")  # USD per million tokens (models.toml)


# --------------------------------------------------------------------------------------------- expected values
def expected(session: Any, spec: dict[str, Any]) -> dict[str, Any] | None:
    """Compute what a correct answer must contain, straight from the tool: {"entities": [...], "value": number|None}."""
    res = tools.run_tool(session, spec["tool"], json.dumps(spec["args"]), {}, "g")
    if "error" in res:
        return None
    op = spec["op"]
    if op == "value":
        v = res["values"].get(spec["key"])
        return None if v is None else {"entities": [], "value": float(v)}
    rows = res["rows"]
    if op == "row":
        row = next((r for r in rows if all(r.get(k) == v for k, v in spec["where"].items())), None)
        if row is None or row.get(spec["field"]) is None:
            return None
        return {"entities": [], "value": float(row[spec["field"]])}
    rows = [r for r in rows if isinstance(r.get(spec["field"]), (int, float))]
    if not rows:
        return None
    best = (min if op == "argmin" else max)(r[spec["field"]] for r in rows)
    winners = [r for r in rows if r[spec["field"]] == best]
    names: list[str] = []
    for r in winners:
        names += [str(r.get(k)) for k in ("group", "district", "label") if r.get(k)]
    entities = [n.lower().replace("_", " ") for n in names]
    entities += [n.split(" (")[0].lower() for n in entities]
    return {"entities": sorted(set(entities)), "value": float(best)}


_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}
_ALIASES = {"female": ("female", "women", "woman"), "male": ("male", "men", "man")}


def number_word(n: float) -> str | None:
    """English word for a whole number below 100 (the answer may spell a count out), else None."""
    if not float(n).is_integer() or not 0 <= n < 100:
        return None
    n = int(n)
    return _ONES[n] if n < 20 else _TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else "")


def mentions_value(answer: str, value: float) -> bool:
    word = number_word(value)
    if word and re.search(rf"\b{word}\b", answer.lower().replace("-", " ")):
        return True
    for _tok, v, _d, tol in numbers_in(answer):
        if abs(v - value) <= tol + 1e-9 or (0 <= value <= 1 and abs(v - value * 100) <= tol + 1e-9):
            return True
    return False


def judge(item: dict[str, Any], out: Any, exp: dict[str, Any] | None) -> dict[str, Any]:
    """Deterministic scoring of one answer."""
    if item["kind"] == "refuse":
        return {"correct": bool(out.refused), "reason": "refused" if out.refused else "answered a question that must be refused"}
    if out.refused:
        return {"correct": False, "reason": "refused an answerable question"}
    if exp is None:
        return {"correct": None, "reason": "expected value unavailable (no data for this question)"}
    text = out.answer.lower().replace("_", " ")
    if item["expect"]["op"] in ("argmin", "argmax"):  # naming the right entity is the question; the value is a bonus
        ok = any(re.search(rf"\b{re.escape(a)}\b", text) for e in exp["entities"] for a in _ALIASES.get(e, (e,)))
    else:
        ok = mentions_value(out.answer, exp["value"])
    return {"correct": bool(ok), "reason": "ok" if ok else f"expected {exp['entities'] or ''} {exp['value']}"}


# --------------------------------------------------------------------------------------------- estimate
def cmd_estimate(args: argparse.Namespace) -> int:
    """Tokens and dollars per question from the real prompt and real tool results (no model call)."""
    sys_tokens = int(len(system_prompt()) * TOKENS_PER_CHAR)
    sample = [("get_geography", {"level": "district"}), ("get_programme_stats", {}), ("get_overview_kpis", {}), ("get_funnel", {})]
    sizes = []
    with get_sessionmaker()() as s:
        for name, a in sample:
            out = tools.run_tool(s, name, json.dumps(a), {}, "c1")
            sizes.append(int(len(json.dumps(out, default=str, separators=(",", ":"))) * TOKENS_PER_CHAR))
    result_tokens = round(statistics.mean(sizes))
    q = 90  # first user message: question plus framing
    out_tokens, think = 260, 400  # step JSON for a tool call is short; the answer step is the long one; effort low thinking
    scenarios = {}
    for name, calls in (("one_tool_round", 2), ("two_tool_rounds", 3)):
        inp = [sys_tokens + q + i * (result_tokens + 120) for i in range(calls)]  # prefix grows by the previous results
        cache_hit_in = [sys_tokens if i else 0 for i in range(calls)]
        fresh = sum(inp) - sum(cache_hit_in)
        cost = (Decimal(fresh) * HAIKU_IN + Decimal(sum(cache_hit_in)) * HAIKU_CACHE_READ + Decimal(calls * (out_tokens + think)) * HAIKU_OUT) / Decimal(1_000_000)
        scenarios[name] = {"model_calls": calls, "input_tokens": sum(inp), "cached_read_tokens_if_cache_hits": sum(cache_hit_in), "output_tokens_incl_thinking": calls * (out_tokens + think),
                           "usd_per_question": float(cost.quantize(Decimal("0.000001"))), "usd_per_100_questions": float((cost * 100).quantize(Decimal("0.0001")))}  # fmt: skip
    print(json.dumps({"system_prompt_tokens": sys_tokens, "avg_tool_result_tokens": result_tokens, "model": "claude-haiku-5-5 (config/models.toml, effort low)", "scenarios": scenarios,
                      "note": "No cache credit is assumed for the first call; if the static prefix is below the provider's minimum cacheable length, real cost is the cached_read-free figure."}, indent=2))  # fmt: skip
    return 0


# --------------------------------------------------------------------------------------------- live eval
async def _run_all(items: list[dict[str, Any]], budget: Decimal) -> list[dict[str, Any]]:
    settings = get_ai_settings().model_copy(update={"anticipa_llm_cache": False})
    sessions = get_sessionmaker()
    router = build_router(settings=settings, sink=DbSink(sessions), run_budget=budget)
    user = CurrentUser(id=uuid.uuid4(), email=None, role="observer")
    rows = []
    try:
        for item in items:
            with sessions() as s:
                exp = expected(s, item["expect"]) if item["kind"] == "answer" else None
                service.reset_state()
                t0 = time.monotonic()
                try:
                    out = await service.answer_question(s, user, AskRequest(question=item["question"]), router=router)
                    err = None
                except Exception as e:  # noqa: BLE001 - one failed question must not stop the run
                    out, err = None, f"{type(e).__name__}: {e}"[:200]
            if out is None:
                rows.append({"id": item["id"], "question": item["question"], "kind": item["kind"], "correct": False, "error": err, "latency_ms": int((time.monotonic() - t0) * 1000)})
                continue
            verdict = judge(item, out, exp)
            rows.append({"id": item["id"], "question": item["question"], "kind": item["kind"], **verdict, "grounded": out.grounded, "refused": out.refused, "iterations": out.iterations,
                         "latency_ms": out.latency_ms, "cost_usd": out.cost_usd, "answer": out.answer, "has_chart": out.chart is not None, "has_table": out.table is not None,
                         "tools": [x["tool"] for x in out.sources], "expected": exp})  # fmt: skip
    finally:
        await router.aclose()
    return rows


def cmd_eval(args: argparse.Namespace) -> int:
    items = json.loads(GOLDEN.read_text(encoding="utf-8"))["questions"]
    if args.only:
        items = [i for i in items if i["id"] in set(args.only.split(","))]
    service.check_rate_limit = lambda *a, **k: None  # the eval is not a user
    started = time.time()
    rows = asyncio.run(_run_all(items, Decimal(str(args.budget))))
    ans = [r for r in rows if r["kind"] == "answer" and r.get("correct") is not None]
    ref = [r for r in rows if r["kind"] == "refuse"]
    lat = sorted(r["latency_ms"] for r in rows)
    with get_sessionmaker()() as s:
        usage = s.execute(select(func.coalesce(func.sum(ModelInvocation.input_tokens), 0), func.coalesce(func.sum(ModelInvocation.output_tokens), 0),
                                 func.coalesce(func.sum(ModelInvocation.cache_read_tokens), 0), func.coalesce(func.sum(ModelInvocation.cache_write_tokens), 0), func.count(),
                                 func.coalesce(func.sum(ModelInvocation.cost_usd), 0))
                          .where(ModelInvocation.task == service.TASK, ModelInvocation.created_at >= func.to_timestamp(started))).one()  # fmt: skip
    report = {
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)), "prompt_version": service.PROMPT_REF, "model": "claude-haiku-5-5", "questions": len(rows),
        "accuracy": round(sum(1 for r in ans if r["correct"]) / len(ans), 4) if ans else None, "answerable": len(ans),
        "grounded_rate": round(sum(1 for r in rows if r.get("grounded")) / len(rows), 4) if rows else None,
        "refusal_correctness": round(sum(1 for r in ref if r["correct"]) / len(ref), 4) if ref else None, "refusal_cases": len(ref),
        "latency_ms": {"p50": lat[len(lat) // 2] if lat else None, "p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None, "max": lat[-1] if lat else None},
        "cost_usd": {"total": round(sum(r.get("cost_usd", 0) for r in rows), 6), "per_question": round(sum(r.get("cost_usd", 0) for r in rows) / len(rows), 6) if rows else None,
                     "per_100_questions": round(100 * sum(r.get("cost_usd", 0) for r in rows) / len(rows), 4) if rows else None},
        "tokens": {"model_calls": usage[4], "input": int(usage[0]), "output": int(usage[1]), "cache_read": int(usage[2]), "cache_write": int(usage[3]), "recorded_cost_usd": float(usage[5])},
        "failures": [{k: r.get(k) for k in ("id", "question", "reason", "answer", "error")} for r in rows if r.get("correct") is not True],
        "results": rows,
    }  # fmt: skip
    out = Path(args.out) if args.out else REPO_ROOT / "data" / "reports" / "ask-eval-20261113.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("results", "failures")}, indent=2, default=str))  # noqa: T201
    print(f"failures: {len(report['failures'])}; report: {out}")  # noqa: T201
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m anticipa.ask", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("estimate", help="tokens and cost per question from the real prompt and tool results (no model call)").set_defaults(func=cmd_estimate)
    ev = sub.add_parser("eval", help="live golden-set evaluation (paid, hard run budget)")
    ev.add_argument("--budget", type=float, default=1.0, help="hard run budget in USD (default 1.00)")
    ev.add_argument("--out", default=None)
    ev.add_argument("--only", default=None, help="comma separated golden ids")
    ev.set_defaults(func=cmd_eval)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
