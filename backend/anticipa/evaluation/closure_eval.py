"""Loop-closure integrity metrics (Docs/09 §7.8 LC-*) plus the communication-language check (EQ-08).

Callers: anticipa.evaluation.runner. Everything is derived from persisted rows; `probe_invalid_closures` additionally
drives the REAL closure service inside transactions that are always rolled back (no row is ever changed). All
engagement and closure data is SIMULATED. Instruction: deliver a working, tested, demonstrable Anticipa MVP including
the evaluation report.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from anticipa.evaluation.stats import rate

OPEN_STATUSES_EXCLUDED = ("completed", "closed_not_completed", "cancelled")


def evaluate_closure(rows: Mapping[str, Any], residents: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    cases = rows["cases"]
    verified: set[tuple[Any, str]] = {
        (e["care_gap_case_id"], e["activity"]) for e in rows["evidence"] if e["verification_status"] == "verified"
    }
    completed = [c for c in cases if c["status"] == "completed"]
    bad = [str(c["id"]) for c in completed if (c["id"], c["care_activity"]) not in verified]
    with_hist = [c for c in cases if c["id"] in rows["history_last"]]
    replay_bad = [str(c["id"]) for c in with_hist if rows["history_last"][c["id"]] != c["status"]]
    simulated = [*rows["communications"], *rows["appointments"], *rows["evidence"]]
    not_sim = sum(1 for r in simulated if r["synthetic_status"] != "simulated")
    comm = rows["communications"]
    lang_ok = sum(1 for r in comm if (residents.get(str(r["resident_id"])) or {}).get("language") == r["language"])
    appts = rows["appointments"]
    cases_with_comm = {r["care_gap_case_id"] for r in comm}
    cases_with_appt = {r["care_gap_case_id"] for r in appts}
    return {
        "LC-01": {"value": len(bad), "unit": "count", "status": "measured", "n": len(completed), "case_ids": bad[:50],
                  "metric": "completed cases without verified completion evidence for their care activity (must be 0)"},  # fmt: skip
        "LC-03": {"value": len(replay_bad), "unit": "count", "status": "measured", "n": len(with_hist), "case_ids": replay_bad[:50],
                  "metric": "cases whose status differs from the last entry of their append-only status history (must be 0)"},  # fmt: skip
        "LC-04": {"status": "measured", "label": "Simulated", "value": len(cases), "unit": "cases",
                  "funnel": {"cases": len(cases), "with_simulated_outreach": len(cases_with_comm & {c["id"] for c in cases}),
                             "with_appointment": len(cases_with_appt & {c["id"] for c in cases}),
                             "completed": len(completed),
                             "closed_not_completed": sum(1 for c in cases if c["status"] == "closed_not_completed")},  # fmt: skip
                  "metric": "closure funnel (simulated)"},
        "LC-05": {"value": not_sim, "unit": "count", "status": "measured", "n": len(simulated),
                  "metric": "outreach/appointment/completion-evidence rows not labelled simulated (must be 0)"},  # fmt: skip
        "EQ-08": {**rate(lang_ok, len(comm)), "metric": "generated messages in the resident's preferred language"},
        "EQ-09": {**rate(sum(1 for a in appts if a["appointment_status"] == "completed"), len(appts)), "label": "Simulated",
                  "metric": "simulated completion rate (a pipeline-behaviour check, not an equity finding)"},  # fmt: skip
    }


def probe_invalid_closures(sessions: Callable[[], Session], *, sample: int = 20) -> dict[str, Any]:
    """Try to COMPLETE open cases that have no verified evidence, through the real closure service; each attempt must
    raise. Transactions are rolled back, so nothing is persisted either way (LC-02 / INV-09)."""
    from anticipa.caregaps.models import CareGapCase
    from anticipa.closure.models import CompletionEvidence
    from anticipa.closure.service import close_case
    from anticipa.core.audit import Actor
    from anticipa.core.errors import ApiError

    with sessions() as s:
        has_verified = select(CompletionEvidence.care_gap_case_id).where(CompletionEvidence.verification_status == "verified")
        ids = list(s.execute(
            select(CareGapCase.id).where(CareGapCase.status.not_in(OPEN_STATUSES_EXCLUDED), CareGapCase.id.not_in(has_verified))
            .order_by(CareGapCase.id).limit(sample)
        ).scalars())  # fmt: skip
        s.rollback()
    rejected = accepted = 0
    codes: Counter = Counter()
    for cid in ids:
        with sessions() as s:
            try:
                case = s.get(CareGapCase, cid)
                close_case(s, case, outcome="completed", reason="evaluation probe: no evidence", actor=Actor.system())
                accepted += 1
            except ApiError as exc:
                rejected += 1
                codes[exc.code] += 1
            finally:
                s.rollback()
    return {"attempts": len(ids), "rejected": rejected, "accepted_in_error": accepted, "error_codes": dict(codes)}


def probe_metric(probe: Mapping[str, Any]) -> dict[str, Any]:
    if probe["attempts"] == 0:
        return {"value": None, "status": "not_evaluated", "metric": "invalid-closure rejection (probe)",
                "reason": "no open case without verified completion evidence exists to probe"}  # fmt: skip
    return {**rate(probe["rejected"], probe["attempts"]), "metric": "invalid-closure rejection: attempts to complete a case with no verified evidence (rolled back; must be 1.0)",
            "error_codes": probe["error_codes"]}  # fmt: skip
