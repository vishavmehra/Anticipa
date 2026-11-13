# ruff: noqa: E501
"""Ask Anticipa: request, model-step and response shapes (no table: nothing here is persisted except the audit event
and the router's model_invocations rows)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MAX_HISTORY_TURNS = 6
MAX_QUESTION_CHARS = 500
MAX_TURN_CHARS = 1500


class AskTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=MAX_TURN_CHARS)


class AskFilters(BaseModel):
    """Optional scope the UI applies to every tool call (a district or a programme)."""

    district: str | None = Field(None, pattern=r"^[A-Za-z0-9 _.-]{1,40}$")
    programme: str | None = Field(None, pattern=r"^[A-Za-z0-9_]{1,60}$")


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=MAX_QUESTION_CHARS)
    history: list[AskTurn] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)
    filters: AskFilters | None = None

    @field_validator("question")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 3:
            raise ValueError("question is too short")
        return v


# ----------------------------------------------------------------------------------------- what the model returns
class ToolCall(BaseModel):
    tool: str = Field(max_length=60)
    args_json: str = Field("{}", max_length=400)  # a JSON object; parsed and validated server side per tool


class ChartRef(BaseModel):
    """The model picks WHICH tool result and WHICH fields to plot. It never types a plotted number."""

    source: str = Field(max_length=10)  # call id, e.g. "c1"
    kind: Literal["bar", "line"] = "bar"
    x: str = Field(max_length=40)
    y: str = Field(max_length=40)
    title: str = Field(max_length=100)


class TableRef(BaseModel):
    source: str = Field(max_length=10)
    columns: list[str] = Field(max_length=6)
    title: str = Field(max_length=100)


class Step(BaseModel):
    """One model turn: ask for tools, answer, or refuse. Provider-neutral (the Model Router has no native tool API)."""

    action: Literal["call_tools", "answer", "refuse"]
    calls: list[ToolCall] = Field(default_factory=list, max_length=3)
    answer: str = Field("", max_length=1800)
    chart: ChartRef | None = None
    table: TableRef | None = None
    follow_ups: list[str] = Field(default_factory=list, max_length=3)


# ----------------------------------------------------------------------------------------- what the API returns
class AskData(BaseModel):
    answer: str
    chart: dict[str, Any] | None = None
    table: dict[str, Any] | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    grounded: bool
    refused: bool = False
    notice: str | None = None
    model: str | None = None
    cost_usd: float = 0.0
    latency_ms: int = 0
    cached: bool = False
    iterations: int = 0
    synthetic_banner: str = (
        "Demonstration data only: every record is generated or simulated, and no real person is represented."
    )
