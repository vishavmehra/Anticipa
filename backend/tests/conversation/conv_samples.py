"""Scripted provider and offline case facts shared by the resident conversation tests (no network, no keys)."""
# ruff: noqa: E501

from __future__ import annotations

import json

from anticipa.ai.types import AdapterResponse, Usage
from anticipa.conversation.service import Facts

SLOT = {"timezone": "UTC", "clinic": "Test Clinic", "address": None, "service": "Screening"}


def facts(*, appointment: bool = True, alternatives: bool = True) -> Facts:
    return Facts(
        care_activity="colorectal_cancer_screening",
        appointment={"state": "confirmed", "date": "Friday 20 November 2026", "start": "10:15", "end": "10:45", **SLOT} if appointment else None,
        alternatives=[{"ref": "s1", "date": "Monday 23 November 2026", "start": "09:00", "end": "09:30", **SLOT}] if alternatives else [],
    )  # fmt: skip


def step(intent="location_time", reply="", *, needs=False, lang="en", ref=None) -> dict:
    return {"intent": intent, "needs_clinician": needs, "language": lang, "reply": reply, "proposed_slot_ref": ref}


GOOD = step("location_time", "Your appointment is on Friday 20 November 2026 at 10:15 at Test Clinic. Please arrive a little early.")
PROPOSE = step("rescheduling", "I can suggest Monday 23 November 2026 at 09:00 at Test Clinic. A care coordinator will confirm it with you.", ref="s1")


class Script:
    """Provider fake: plays one scripted JSON step per call (the last repeats) and records what it was sent."""

    def __init__(self, *steps: dict | Exception) -> None:
        self.steps, self.calls = list(steps), []

    async def generate(self, call):
        self.calls.append((call.system, [m["content"] for m in call.messages]))
        item = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(item, Exception):
            raise item
        return AdapterResponse(text=json.dumps(item, ensure_ascii=False), usage=Usage(input_tokens=1800, output_tokens=90, cache_read_tokens=1200))

    async def aclose(self):
        return None
