"""Deterministic care gap case priority (ADR-073). The specifications ask the care gap engine to "determine case priority"
but define no rules, so this is the minimal documented rule set. Priority never changes who is eligible; it only orders
work (reviewers, planning) and is recomputed from stored facts, never by a model.

Rules, first match wins (levels: routine < elevated < high):
  1. family_history_pathway      the care activity is a hereditary or family history pathway (BRCA risk assessment, BRCA
                                 genetic counselling referral, colorectal family history)               -> high
  2. clinical_review_needed      the evaluation requires a clinical review (an exclusion or an undecidable criterion)
                                                                                                         -> high
  3. conflicting_evidence        the case is eligible but the evaluation recorded contradicting sources -> elevated
  4. default                     everything else                                                          -> routine
A priority authored on the guideline version is a floor: the derived level is never lower than it.
"Long overdue" is not a rule: the evaluations store no last screening date to measure it from.

`python -m anticipa.caregaps.priority recompute [--dry-run] [--force]` re-derives the priority of every open case in
batches (terminal cases keep theirs), writes one audit event per batch and never touches updated_at.
"""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import logging
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from anticipa.caregaps.models import PRIORITIES, TERMINAL_STATUSES, CareGapCase
from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.guidelines.models import EligibilityEvaluation, GuidelineVersion

log = logging.getLogger(__name__)

RULESET = "caregaps.priority/1"
ADR = "ADR-073"
RANK = {p: i for i, p in enumerate(PRIORITIES)}  # routine 0, elevated 1, high 2
FAMILY_HISTORY_MARKERS = ("brca", "family_history")
BUSY_JOB_PREFIXES = ("guidelines.", "caregaps.", "planning.")
CHUNK = 5000


def derive_priority(care_activity: str, outcome: str, has_conflicts: bool = False, floor: str | None = "routine") -> tuple[str, str]:
    """(priority, rule) for one case. `floor` is the guideline version's own priority."""
    activity = (care_activity or "").lower()
    if any(m in activity for m in FAMILY_HISTORY_MARKERS):
        level, rule = "high", "family_history_pathway"
    elif outcome == "requires_clinical_review":
        level, rule = "high", "clinical_review_needed"
    elif has_conflicts:
        level, rule = "elevated", "conflicting_evidence"
    else:
        level, rule = "routine", "default"
    if floor in RANK and RANK[floor] > RANK[level]:
        return floor, "guideline_priority"
    return level, rule


def has_conflicts_column() -> Any:
    return (func.jsonb_array_length(EligibilityEvaluation.conflicting_information) > 0).label("has_conflicts")


def recompute_open_priorities(
    sessions: Callable[[], Session], *, actor: Actor | None = None, chunk_size: int = CHUNK, dry_run: bool = False
) -> dict[str, Any]:
    """Re-derive the priority of every open case, in keyset batches of `chunk_size`; one audit event per batch with changes."""
    actor = actor or Actor.system()
    totals: Counter[str] = Counter()
    moves: Counter[str] = Counter()
    last: uuid.UUID | None = None
    while True:
        with sessions() as s:
            stmt = (
                select(CareGapCase.id, CareGapCase.priority, CareGapCase.care_activity, CareGapCase.source_outcome, GuidelineVersion.priority.label("floor"), has_conflicts_column())
                .join(EligibilityEvaluation, EligibilityEvaluation.id == CareGapCase.evaluation_id)
                .join(GuidelineVersion, GuidelineVersion.id == CareGapCase.guideline_version_id)
                .where(CareGapCase.status.notin_(TERMINAL_STATUSES))
                .order_by(CareGapCase.id)
                .limit(chunk_size)
            )
            if last is not None:
                stmt = stmt.where(CareGapCase.id > last)
            rows = s.execute(stmt).all()
            if not rows:
                break
            ids, new_levels, batch = [], [], Counter()
            for r in rows:
                level, _rule = derive_priority(r.care_activity, r.source_outcome, bool(r.has_conflicts), r.floor)
                totals[level] += 1
                if level != r.priority:
                    ids.append(r.id)
                    new_levels.append(level)
                    batch[f"{r.priority}->{level}"] += 1
            if ids and not dry_run:
                s.execute(
                    text("UPDATE care_gap_cases c SET priority = v.p FROM unnest(CAST(:ids AS uuid[]), CAST(:ps AS text[])) AS v(id, p) WHERE c.id = v.id"),
                    {"ids": ids, "ps": new_levels},
                )  # updated_at is deliberately left alone: the backlog alert measures time since the last status change
                audit.record(s, actor, "caregap.priority_recomputed", "care_gap_case", None, "success",
                             {"ruleset": RULESET, "adr": ADR, "cases": len(rows), "changed": len(ids), "moves": dict(batch)})  # fmt: skip
            s.commit()
        moves.update(batch)
        last = rows[-1].id
        totals["_scanned"] += len(rows)
        totals["_changed"] += len(ids)
        if len(rows) < chunk_size:
            break
    return {"ruleset": RULESET, "dry_run": dry_run, "scanned": totals.pop("_scanned", 0), "changed": totals.pop("_changed", 0),
            "moves": dict(moves), "distribution_after": {p: totals.get(p, 0) for p in PRIORITIES}}  # fmt: skip


def busy_jobs(session: Session) -> list[str]:
    """Running or queued jobs of modules that create or change cases (a recompute must not run beside them)."""
    rows = session.execute(
        text("SELECT job_type FROM processing_jobs WHERE status IN ('pending', 'queued', 'running')")
    ).scalars().all()  # fmt: skip
    return sorted({j for j in rows if j.startswith(BUSY_JOB_PREFIXES)})


def distribution(session: Session, statuses_open: bool = True) -> dict[str, int]:
    stmt = select(CareGapCase.priority, func.count()).group_by(CareGapCase.priority)
    if statuses_open:
        stmt = stmt.where(CareGapCase.status.notin_(TERMINAL_STATUSES))
    return {p: n for p, n in session.execute(stmt).all()}


def main(argv: Sequence[str] | None = None) -> int:
    from anticipa.db import get_sessionmaker

    p = argparse.ArgumentParser(prog="python -m anticipa.caregaps.priority", description="Deterministic case priority (ADR-073)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recompute", help="re-derive the priority of every open case in batches")
    r.add_argument("--dry-run", action="store_true", help="count what would change, write nothing")
    r.add_argument("--force", action="store_true", help="run even when case creating jobs are queued or running")
    r.add_argument("--chunk-size", type=int, default=CHUNK)
    args = p.parse_args(argv)
    sessions = get_sessionmaker()
    with sessions() as s:
        busy = busy_jobs(s)
    if busy and not args.force and not args.dry_run:
        print(json.dumps({"refused": "jobs that create or change cases are queued or running", "job_types": busy, "hint": "wait for them or pass --force"}))
        return 2
    out = recompute_open_priorities(sessions, chunk_size=args.chunk_size, dry_run=args.dry_run)
    with sessions() as s:
        out["open_cases_by_priority"] = distribution(s)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
