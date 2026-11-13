"""Scripted provider and canned tool results shared by the Ask Anticipa tests."""
# ruff: noqa: E501

from __future__ import annotations

import json

from anticipa.ai.types import AdapterResponse, Usage

GEO = {
    "call_id": "c1", "tool": "get_geography", "args": {"level": "district"}, "definition": "closure = completed / cases", "source": "simulated",
    "values": {"areas": 2},
    "rows": [
        {"group": "D02", "label": "Indore (D02)", "cases": 12000, "closure_rate": 0.0412},
        {"group": "D05", "label": "Pune (D05)", "cases": 9000, "closure_rate": 0.0089},
    ],
    "labels": {"label": "District", "closure_rate": "Closure rate"}, "units": {"closure_rate": "ratio", "cases": "count"}, "notes": [],
}  # fmt: skip
CALL = {"action": "call_tools", "calls": [{"tool": "get_geography", "args_json": '{"level":"district"}'}]}
GOOD = {
    "action": "answer", "answer": "Pune (D05) has the lowest closure rate at 0.9%, against 4.1% in Indore (D02).",
    "chart": {"source": "c1", "kind": "bar", "x": "label", "y": "closure_rate", "title": "Closure rate by district"},
    "follow_ups": ["How full is capacity in Pune?"],
}  # fmt: skip
BAD = {**GOOD, "answer": "Pune (D05) has the lowest closure rate at 0.7%."}


class Script:
    """Provider fake that plays one scripted JSON step per call (the last repeats) and records what it was sent."""

    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    async def generate(self, call):
        self.calls.append((call.system, [m["content"] for m in call.messages]))
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        return AdapterResponse(text=json.dumps(step), usage=Usage(input_tokens=2500, output_tokens=120, cache_read_tokens=1800))

    async def aclose(self):
        return None
