"""The planner turns a case's care_activity into a clinic service key (identity unless mapped).

Every care activity the shipped guideline drafts produce must map to a service the clinic network offers."""

from __future__ import annotations

import json
import re
from pathlib import Path

from anticipa.planning.adapters import CARE_ACTIVITY_SERVICE_KEYS
from anticipa.planning.routes import KEY
from anticipa.planning.synthetic import SERVICE_CATALOGUE

DRAFT = Path(__file__).resolve().parents[3] / "config" / "guidelines" / "rules_draft.json"
INDIA_DRAFT = Path(__file__).resolve().parents[3] / "config" / "guidelines" / "rules_india_draft.json"


def test_every_care_activity_is_a_valid_planner_service_key():
    guidelines = json.loads(DRAFT.read_text(encoding="utf-8"))["guidelines"]
    guidelines += json.loads(INDIA_DRAFT.read_text(encoding="utf-8"))["guidelines"]
    activities = [
        g["guideline_key"].replace("-", "_") for g in guidelines
    ]  # what service.create_version stores
    assert len(set(activities)) == len(activities) == 11  # 6 US drafts + 5 India national programme drafts
    for activity in activities:
        service_key = CARE_ACTIVITY_SERVICE_KEYS.get(activity, activity)
        assert re.match(KEY, service_key), (activity, service_key)
        assert service_key in SERVICE_CATALOGUE, (activity, service_key)
