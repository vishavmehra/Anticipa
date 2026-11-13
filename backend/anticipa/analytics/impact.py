"""Serving side of the proven impact report (gap audit B02, B08, B09 data half). No computation, no ground truth.

The report is computed by `python -m anticipa.evaluation impact` (anticipa/evaluation/impact.py: it reads planted ground truth,
so pipeline and serving modules never import it, ADR-008). This module only finds it: the newest
`data/reports/impact-baselines-*.json` (re-read when the file changes), or, on a deployment without `data/`, the copy that
`--store` keeps in `analytics_snapshots` (key `impact`, aggregates only). GET /analytics/impact serves it.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from anticipa.analytics.models import AnalyticsSnapshot
from anticipa.config import ROOT_DIR

REPORTS_DIR = ROOT_DIR / "data" / "reports"
REPORT_GLOB = "impact-baselines-*.json"
STORE_KEY = "impact"
_CACHE: dict[str, Any] = {}


def load_report(session: Session | None = None) -> dict[str, Any]:
    """The newest report file, else the stored copy; FileNotFoundError when there is neither."""
    files = sorted(REPORTS_DIR.glob(REPORT_GLOB))
    if files:
        key = (str(files[-1]), files[-1].stat().st_mtime_ns)
        if _CACHE.get("key") != key:
            _CACHE.update(key=key, data=json.loads(files[-1].read_text(encoding="utf-8")))
        return _CACHE["data"]
    if session is not None:
        try:
            with session.begin_nested():
                stored = session.scalar(select(AnalyticsSnapshot.payload).where(AnalyticsSnapshot.key == STORE_KEY))
        except DBAPIError:
            stored = None
        if stored:
            return stored
    raise FileNotFoundError("no impact report yet: run `python -m anticipa.evaluation impact`")
