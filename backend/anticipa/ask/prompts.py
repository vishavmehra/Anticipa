# ruff: noqa: E501
"""The static system prompt: the versioned template plus the tool catalogue. Deterministic text (no clock, no ids),
so the provider's prompt cache keeps hitting it (the Claude adapter marks the system block as a cache breakpoint)."""

from __future__ import annotations

from functools import lru_cache

from anticipa.ai.prompts import get_prompt
from anticipa.ask.tools import tool_catalogue

PROMPT_REF = "ask.analytics@1"


@lru_cache(maxsize=1)
def system_prompt() -> str:
    return get_prompt(PROMPT_REF).text.replace("{{TOOLS}}", tool_catalogue())
